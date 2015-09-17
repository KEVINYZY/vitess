# Copyright 2012, Google Inc. All rights reserved.
# Use of this source code is governed by a BSD-style license that can
# be found in the LICENSE file.

"""VTGateCursor, and StreamVTGateCursor."""

import itertools
import operator
import re

from vtdb import base_cursor
from vtdb import dbexceptions

write_sql_pattern = re.compile(r'\s*(insert|update|delete)', re.IGNORECASE)


def ascii_lower(string):
    """Lower-case, but only in the ASCII range."""
    return string.encode('utf8').lower().decode('utf8')


class VTGateCursorMixin(object):

  def connection_list(self):
    return [self._conn]

  def is_writable(self):
    return self._writable


class VTGateCursor(base_cursor.BaseListCursor, VTGateCursorMixin):
  """A cursor for execute statements to VTGate.

  Results are stored as a list.
  """

  def __init__(
      self, connection, keyspace, tablet_type, keyspace_ids=None,
      keyranges=None, writable=False, as_transaction=False):
    """Init VTGateCursor.

    Args:
      connection: A PEP0249 connection object.
      keyspace: Str keyspace or None if batch API will be used.
      tablet_type: Str tablet_type.
      keyspace_ids: Struct('!Q').packed keyspace IDs.
      keyranges: Str keyranges.
      writable: True if writable.
    """
    super(VTGateCursor, self).__init__()
    self._conn = connection
    self._writable = writable
    self.description = None
    self.index = None
    self.keyranges = keyranges
    self.keyspace = keyspace
    self.keyspace_ids = keyspace_ids
    self.lastrowid = None
    self.results = None
    self.routing = None
    self.rowcount = 0
    self.tablet_type = tablet_type
    self.as_transaction = as_transaction
    self._clear_batch_state()

  # pass kargs here in case higher level APIs need to push more data through
  # for instance, a key value for shard mapping
  def execute(self, sql, bind_variables, **kargs):
    self._clear_list_state()
    if self._handle_transaction_sql(sql):
      return
    write_query = bool(write_sql_pattern.match(sql))
    # NOTE: This check may also be done at higher layers but adding it
    # here for completion.
    if write_query:
      if not self.is_writable():
        raise dbexceptions.DatabaseError('DML on a non-writable cursor', sql)

    self.results, self.rowcount, self.lastrowid, self.description = (
        self.connection._execute(
            sql,
            bind_variables,
            self.keyspace,
            self.tablet_type,
            keyspace_ids=self.keyspace_ids,
            keyranges=self.keyranges,
            not_in_transaction=not self.is_writable(),
            effective_caller_id=self.effective_caller_id))
    return self.rowcount

  def execute_entity_ids(
      self, sql, bind_variables, entity_keyspace_id_map, entity_column_name):
    self._clear_list_state()

    # This is by definition a scatter query, so raise exception.
    write_query = bool(write_sql_pattern.match(sql))
    if write_query:
      raise dbexceptions.DatabaseError(
          'execute_entity_ids is not allowed for write queries')
    self.results, self.rowcount, self.lastrowid, self.description = (
        self.connection._execute_entity_ids(
            sql,
            bind_variables,
            self.keyspace,
            self.tablet_type,
            entity_keyspace_id_map,
            entity_column_name,
            not_in_transaction=not self.is_writable(),
            effective_caller_id=self.effective_caller_id))
    return self.rowcount

  def fetch_aggregate_function(self, func):
    return func(row[0] for row in self.fetchall())

  def fetch_aggregate(self, order_by_columns, limit):
    sort_columns = []
    desc_columns = []
    for order_clause in order_by_columns:
      if type(order_clause) in (tuple, list):
        sort_columns.append(order_clause[0])
        if ascii_lower(order_clause[1]) == 'desc':
          desc_columns.append(order_clause[0])
      else:
        sort_columns.append(order_clause)
    # sort the rows and then trim off the prepended sort columns

    if sort_columns:
      sorted_rows = list(sort_row_list_by_columns(
          self.fetchall(), sort_columns, desc_columns))[:limit]
    else:
      sorted_rows = itertools.islice(self.fetchall(), limit)
    neutered_rows = [row[len(order_by_columns):] for row in sorted_rows]
    return neutered_rows

  def _clear_batch_state(self):
    """Clear state that allows traversal to next query's results."""
    self.result_sets = []
    self.result_set_index = None

  def close(self):
    super(VTGateCursor, self).close()
    self._clear_batch_state()

  def executemany(self, sql, params_list):
    """Execute multiple statements in one batch.

    This adds len(params_list) result_sets to self.result_sets.  Each
    result_set is a (results, rowcount, lastrowid, fields) tuple.

    Each call overwrites the old result_sets. After execution, nextset()
    is called to move the fetch state to the start of the first
    result set.

    Args:
      sql: The sql text, with %(format)s-style tokens. May be None.
      params_list: A list of the keyword params that are normally sent
        to execute. Either the sql arg or params['sql'] must be defined.

    """
    if sql:
      sql_list = [sql] * len(params_list)
    else:
      sql_list = [params['sql'] for params in params_list]
    bind_variables_list = [params['bind_variables'] for params in params_list]
    keyspace_list = [params['keyspace'] for params in params_list]
    keyspace_ids_list = [params['keyspace_ids'] for params in params_list]
    self._clear_batch_state()
    self.result_sets = self.connection._execute_batch(
        sql_list, bind_variables_list, keyspace_list, keyspace_ids_list,
        self.tablet_type, self.as_transaction, self.effective_caller_id)
    self.nextset()

  def nextset(self):
    """Move the fetch state to the start of the next result set.

    self.(results, rowcount, lastrowid, description) will be set to
    the next result_set, and the fetch-commands will work on this
    result set.

    Returns:
      True if another result set exists, False if not.
    """
    if self.result_set_index is None:
      self.result_set_index = 0
    else:
      self.result_set_index += 1

    self._clear_list_state()
    if self.result_set_index < len(self.result_sets):
      self.results, self.rowcount, self.lastrowid, self.description = (
          self.result_sets[self.result_set_index])
      return True
    else:
      self._clear_batch_state()
      return None


class StreamVTGateCursor(base_cursor.BaseStreamCursor, VTGateCursorMixin):

  """A cursor for streaming statements to VTGate.

  Results are returned as a generator.
  """

  def __init__(
      self, connection, keyspace, tablet_type, keyspace_ids=None,
      keyranges=None, writable=False):
    super(StreamVTGateCursor, self).__init__()
    self._conn = connection
    self._writable = writable
    self.keyranges = keyranges
    self.keyspace = keyspace
    self.keyspace_ids = keyspace_ids
    self.routing = None
    self.tablet_type = tablet_type

  def is_writable(self):
    return self._writable

  # pass kargs here in case higher level APIs need to push more data through
  # for instance, a key value for shard mapping
  def execute(self, sql, bind_variables, **kargs):
    if self._writable:
      raise dbexceptions.ProgrammingError('Streaming query cannot be writable')
    self._clear_stream_state()
    self.generator, self.description = self.connection._stream_execute(
        sql,
        bind_variables,
        self.keyspace,
        self.tablet_type,
        keyspace_ids=self.keyspace_ids,
        keyranges=self.keyranges,
        not_in_transaction=not self.is_writable(),
        effective_caller_id=self.effective_caller_id)
    return 0


# assumes the leading columns are used for sorting
def sort_row_list_by_columns(row_list, sort_columns=(), desc_columns=()):
  for column_index, column_name in reversed(
      [x for x in enumerate(sort_columns)]):
    og = operator.itemgetter(column_index)
    if type(row_list) != list:
      row_list = sorted(
          row_list, key=og, reverse=bool(column_name in desc_columns))
    else:
      row_list.sort(key=og, reverse=bool(column_name in desc_columns))
  return row_list
