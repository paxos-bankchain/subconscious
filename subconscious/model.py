# python modules
import asyncio
import inspect
import logging

from datetime import datetime
from itertools import product

from .column import Column


logger = logging.getLogger(__name__)

VALUE_ID_SEPARATOR = '\x00'
MODEL_NAME_ID_SEPARATOR = ':'


# Exceptions

class InvalidQuery(Exception):
    pass


class InvalidModelDefinition(Exception):
    pass


class BadDataError(Exception):
    pass


class UnexpectedColumnError(Exception):
    pass


class ModelMeta(type):

    def __init__(cls, what, bases=None, attributes=None):
        super(ModelMeta, cls).__init__(what, bases, attributes)
        if cls.__name__ not in ('RedisModel', 'TimeStampedModel'):
            columns = []
            num_primary, num_composite = 0, 0
            cls._pk_name = None
            # grab all Columns from the model
            for name, column in inspect.getmembers(cls, lambda col: isinstance(col, Column)):
                column.name = name
                columns.append(column)
                if column.primary:
                    num_primary += 1
                    cls._pk_name = column.name
                if column.composite:
                    num_composite += 1

            # Defensive checks
            if num_primary == 0:
                if num_composite == 0:
                    err_msg = 'No primary key or composite key in {}'.format(cls.__name__)
                    raise InvalidModelDefinition(err_msg)
                if num_composite == 1:
                    err_msg = 'Your composite key is really a primary key in {}'.format(cls.__name__)
                    raise InvalidModelDefinition(err_msg)
            if num_primary == 1:
                if num_composite != 0:
                    err_msg = 'Cannot have both primary and composite keys in {}'.format(cls.__name__)
                    raise InvalidModelDefinition(err_msg)

            cls._columns = tuple(sorted(columns, key=lambda c: c.name))
            cls._indexed_columns = tuple(sorted([col for col in cls._columns if col.indexed], key=lambda c: c.name))
            cls._sortable_columns = tuple(sorted([col for col in cls._columns if col.sorted], key=lambda c: c.name))
            cls._identifier_columns = tuple(
                sorted([col for col in cls._columns if col.primary or col.composite],
                       key=lambda c: c.name))
            cls._auto_columns = sorted(
                [col for col in cls._columns if getattr(col, 'auto_increment', False)],
                key=lambda c: c.name
            )
            cls._queryable_colnames_set = set([col.name for col in cls._indexed_columns + cls._identifier_columns])
            cls._sortable_column_names = tuple([x.name for x in cls._sortable_columns])
            cls._auto_column_names = {col.name for col in cls._auto_columns}
            cls._indexed_column_names = {col.name for col in cls._indexed_columns}


class RedisModel(object, metaclass=ModelMeta):

    # force only keyword arguments
    def __init__(self, **kwargs):
        loading = kwargs.pop('loading', False)
        for column in self._columns:
            if column.name in kwargs:
                value = kwargs.pop(column.name)
                if type(value) != column.field_type:
                    err_msg = "Column `{}` in {} has value {}, should be of type {}".format(
                        column.name,
                        self.__class__.__name__,
                        value,
                        column.field_type,
                    )
                    raise BadDataError(err_msg)

                if column.enum_choices and value not in column.enum_choices:
                    err_msg = "Column `{}` in {} has value {}, should be in set {}".format(
                        column.name,
                        self.__class__.__name__,
                        value,
                        column.enum_choices,
                    )
                    raise BadDataError(err_msg)
                if getattr(column, 'auto_increment', False) and not loading:
                    err_msg = "Not allowed to set auto_increment column({})".format(column.name)
                    raise BadDataError(err_msg)

                self.__dict__.update({column.name: value})
            else:
                if column.required and not getattr(column, 'auto_increment', False):
                    err_msg = 'Missing column `{}` in `{}` is required'.format(
                        column.name,
                        self.__class__.__name__,
                    )
                    raise BadDataError(err_msg)

        # Require that every kwarg supplied matches an expected column
        # TODO: handle TimeStampedModel cols better
        known_cols_set = set([column.name for column in self._columns] + ['updated_at', 'created_at'])
        supplied_cols_set = set([x for x in kwargs])
        unknown_cols_set = supplied_cols_set - known_cols_set
        if unknown_cols_set != set():
            err_msg = 'Unknown column(s): {} in `{}`'.format(
                unknown_cols_set,
                self.__class__.__name__,
            )
            raise UnexpectedColumnError(err_msg)

    def __setattr__(self, name, value):
        if name in self._auto_column_names:
            err_msg = "Not allowed to set auto_increment column({})".format(name)
            raise BadDataError(err_msg)

        return super(RedisModel, self).__setattr__(name, value)

    @classmethod
    def key_prefix(cls):
        """Prefix that we use for Redis storage, used for all keys related
        to this object. Default to class name.
        """
        return cls.__name__

    @classmethod
    def make_key(cls, identifier):
        """Convenience method for computing the Redis object instance key
        from the identifier
        """
        return "{}{}{}".format(cls.key_prefix(), MODEL_NAME_ID_SEPARATOR, identifier)

    def has_real_data(self, column_name):
        return not isinstance(getattr(self, column_name), Column)

    def identifier(self):
        identifiers = [str(getattr(self, column.name)) for column in self._identifier_columns]
        return ':'.join(identifiers)

    def redis_key(self):
        """Key used for storage of object instance in Redis.
        """
        return "{}{}{}".format(self.key_prefix(), MODEL_NAME_ID_SEPARATOR, self.identifier())

    def as_dict(self):
        """Dict version of this object
        """
        # WARNING: we have to send a copy, otherwise changing the dict
        # changes the object!
        # FIXME: this returns no keys for keys whose value is None!
        return self.__dict__.copy()

    def __repr__(self):
        return "<{}>".format(self.redis_key())

    def get_index_redis_key(self):
        key_components = ['index', self.key_prefix()]
        for column in self._indexed_columns:
            key_components.append(str(getattr(self, column.name)))
        return MODEL_NAME_ID_SEPARATOR.join(key_components)

    @classmethod
    def get_sort_column_key(cls, column_name):
        return 'sort{}{}{}{}'.format(MODEL_NAME_ID_SEPARATOR, cls.key_prefix(), MODEL_NAME_ID_SEPARATOR, column_name)

    async def save_index(self, db, stale_object=None):
        current_index_redis_key = self.get_index_redis_key()
        for indexed_column in set(list(self._sortable_column_names) + list(self._indexed_column_names)):
            # if self.has_real_data(indexed_column):
            # Index it by adding to a sorted set with 0 score. It will be lexically sorted by redis
            index_key = self.get_sort_column_key(indexed_column)
            if stale_object:
                stale_index_value = '{}{}{}'.format(
                    getattr(stale_object, indexed_column),
                    VALUE_ID_SEPARATOR,
                    stale_object.identifier()
                )
                await db.zrem(index_key, stale_index_value)
            index_value = '{}{}{}'.format(
                getattr(self, indexed_column),
                VALUE_ID_SEPARATOR,
                self.identifier()
            )
            await db.zadd(index_key, 0, index_value,)

        await db.sadd(current_index_redis_key, self.identifier())
        if stale_object:
            stale_index_redis_key = stale_object.get_index_redis_key()
            # update the index only if they're different
            if stale_index_redis_key != current_index_redis_key:
                await db.srem(stale_index_redis_key, self.identifier())

    async def save(self, db):
        """Save the object to Redis.
        """
        kwargs = {}
        for col in self._auto_columns:
            if not self.has_real_data(col.name):
                kwargs[col.name] = await col.auto_generate(db, self)
        self.__dict__.update(kwargs)

        # we have to delete the old index key
        stale_object = await self.__class__.load(db, identifier=self.identifier())
        success = await db.hmset_dict(self.redis_key(), self.__dict__.copy())
        await self.save_index(db, stale_object=stale_object)
        return success

    async def exists(self, db):
        return await db.exists(self.redis_key())

    @classmethod
    async def load(cls, db, identifier=None, redis_key=None):
        """Load the object from redis. Use the identifier (colon-separated
        composite keys or the primary key) or the redis_key.
        """
        if not identifier and not redis_key:
            raise InvalidQuery('Must supply identifier or redis_key')
        if redis_key is None:
            redis_key = cls.make_key(identifier)
        if await db.exists(redis_key):
            data = await db.hgetall(redis_key)
            kwargs = {}
            for key_bin, value_bin in data.items():
                key, value = key_bin, value_bin
                column = getattr(cls, key, False)
                if not column or (column.field_type == str):
                    kwargs[key] = value
                else:
                    kwargs[key] = column.field_type(value)
            kwargs['loading'] = True
            return cls(**kwargs)
        else:
            logger.debug("No Redis key found: {}".format(redis_key))
            return None

    @classmethod
    async def all_keys(cls, db):
        """Return all redis keys that are in the database for this class.
        """
        return await db.keys("{}{}*".format(cls.key_prefix(), MODEL_NAME_ID_SEPARATOR))

    @classmethod
    async def all(cls, db, order_by=None):
        """Return all object instances of this class that's in the db.
        """
        if order_by:
            if order_by[0] in ['+', '-']:
                direction, order_by = order_by[0], order_by[1:]
            else:
                direction = '+'
            if order_by not in cls._sortable_column_names:
                err_msg = 'order_by `{}` not in {}'.format(order_by, cls._sortable_column_names)
                raise InvalidQuery(err_msg)
            if direction == '+':
                range_func = db.zrange
            else:
                range_func = db.zrevrange
            all_keys = []
            for index_entry in await range_func(cls.get_sort_column_key(order_by), 0, -1):
                all_keys.append('{}{}{}'.format(
                    cls.key_prefix(),
                    MODEL_NAME_ID_SEPARATOR,
                    index_entry.split(VALUE_ID_SEPARATOR)[-1])
                )
        else:
            all_keys = await cls.all_keys(db)
        if not all_keys:
            return []
        futures = []
        for redis_key in all_keys:
            futures.append(cls.load(db, redis_key=redis_key))
        return [x for x in await asyncio.gather(*futures, loop=db.connection._loop)
                if x is not None]

    @classmethod
    async def filter_by(cls, db, **kwargs):
        """Return all object instances of this class that have
        values at columns determined by kwargs
        """

        # Assert that the lookup keys are part of indexed, pk or composite keys
        missing_cols_set = set(kwargs.keys()) - cls._queryable_colnames_set
        if missing_cols_set:
            err_msg = '{missing_cols_set} not in {queryable_cols}'.format(
                missing_cols_set=missing_cols_set,
                queryable_cols=cls._queryable_colnames_set,
            )
            raise InvalidQuery(err_msg)

        # Check if PK is in the lookup field. If yes then do a load and return.
        if cls._pk_name in kwargs:
            entity = await cls.load(db, identifier=kwargs.get(cls._pk_name))
            # Now match with the other fields supplied for lookup
            for key, val in kwargs.items():
                if key == cls._pk_name:
                    continue
                if type(val) == list:
                    if getattr(entity, key) not in val:
                        return []
                elif getattr(entity, key) != val:
                    return []
            return [entity]

        # Make list of unique entries. We need a list to do index ops below
        in_clauses = list({x for x in kwargs.keys() if type(kwargs[x]) == list})
        field_combinations = product(*[kwargs[x] for x in in_clauses])
        index_keys_collection = []
        for field_combo in field_combinations:
            kwargs_copy = kwargs.copy()
            # pop out in-clause fields first. They will be used to construct individual index lookup keys below.
            [kwargs_copy.pop(x) for x in in_clauses]
            # re-set in clause values one at a time.
            for i, field in enumerate(field_combo):
                kwargs_copy[in_clauses[i]] = field
            key_components = ["index", cls.key_prefix()]
            for column in cls._indexed_columns:
                if column.name in kwargs_copy:
                    if kwargs_copy[column.name] is None:
                        # This is the way we are constructing index key in save()
                        # when the indexed field value is None
                        key_components.append(str(column))
                    else:
                        key_components.append(str(kwargs_copy[column.name]))  # Stringify field value
                else:
                    key_components.append('*')
            index_keys_collection.append(MODEL_NAME_ID_SEPARATOR.join(key_components))

        if not index_keys_collection:
            key_components = ["index", cls.key_prefix()]
            for column in cls._indexed_columns:
                if column.name in kwargs:
                    if kwargs[column.name] is None:
                        # This is the way we are constructing index key in save() when the indexed field value is None
                        key_components.append(str(column))
                    else:
                        key_components.append(str(kwargs[column.name]))  # Stringify field value
                else:
                    key_components.append('*')
            index_keys_collection.append(MODEL_NAME_ID_SEPARATOR.join(key_components))

        identifiers = []
        start = datetime.utcnow()
        for all_index_keys in index_keys_collection:
            async for k in db.iscan(match=all_index_keys):
                identifiers.extend(await db.smembers(k))
        logger.debug('Scan loop took {} seconds for {}'.format((datetime.utcnow() - start).total_seconds(),
                                                               index_keys_collection))
        start = datetime.utcnow()
        _futures = [cls.load(db, identifier=p) for p in sorted(identifiers)]
        result = await asyncio.gather(*_futures, loop=db.connection._loop)
        logger.debug('Gathering entities took {} seconds'.format((datetime.utcnow() - start).total_seconds()))
        return result


    @classmethod
    async def find_by(cls, db, **kwargs):
        # Assert that the lookup keys are part of indexed, pk or composite keys
        missing_cols_set = set(kwargs.keys()) - cls._queryable_colnames_set
        if missing_cols_set:
            err_msg = '{missing_cols_set} not in {queryable_cols}'.format(
                missing_cols_set=missing_cols_set,
                queryable_cols=cls._queryable_colnames_set,
            )
            raise InvalidQuery(err_msg)
        # Check if PK is in the lookup field. If yes then do a load and return.
        if cls._pk_name in kwargs:
            entity = await cls.load(db, identifier=kwargs.get(cls._pk_name))
            # Now match with the other fields supplied for lookup
            for key, val in kwargs.items():
                if key == cls._pk_name:
                    continue
                if type(val) == list:
                    if getattr(entity, key) not in val:
                        return []
                elif getattr(entity, key) != val:
                    return []
            return [entity]
        for field, value in kwargs:
            pass

