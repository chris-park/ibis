from __future__ import annotations

import collections
import concurrent.futures
import itertools
import os
import subprocess
from pathlib import Path

import pytest

import ibis
import ibis.expr.types as ir
import ibis.util as util
from ibis import options
from ibis.backends.conftest import TEST_TABLES, lock_load_data
from ibis.backends.impala.compiler import ImpalaCompiler, ImpalaExprTranslator
from ibis.backends.tests.base import (
    BackendTest,
    RoundAwayFromZero,
    UnorderedComparator,
)
from ibis.tests.expr.mocks import MockBackend


class TestConf(UnorderedComparator, BackendTest, RoundAwayFromZero):
    supports_arrays = True
    supports_arrays_outside_of_select = False
    check_dtype = False
    supports_divide_by_zero = True
    returned_timestamp_unit = 's'
    supports_structs = False

    @staticmethod
    def load_data(
        data_dir,
        script_dir,
        with_hdfs: bool = True,
        database: str | None = None,
        **kwargs,
    ):
        """Load testdata into an impala backend.

        Parameters
        ----------
        data_dir : Path
            Location of testdata
        script_dir : Path
            Location of scripts defining schemas
        """
        import fsspec

        # without setting the pool size
        # connections are dropped from the urllib3
        # connection pool when the number of workers exceeds this value.
        # this doesn't appear to be configurable through fsspec
        URLLIB_DEFAULT_POOL_SIZE = 10

        env = IbisTestEnv()
        con = ibis.impala.connect(
            host=env.impala_host,
            port=env.impala_port,
            database=database,
            hdfs_client=fsspec.filesystem(
                env.hdfs_protocol,
                host=env.nn_host,
                port=env.hdfs_port,
                user=env.hdfs_user,
            )
            if with_hdfs
            else None,
            pool_size=URLLIB_DEFAULT_POOL_SIZE,
        )

        fs = fsspec.filesystem("file")

        data_files = {
            data_file
            for data_file in fs.find(data_dir)
            # ignore sqlite databases and markdown files
            if not data_file.endswith((".db", ".md"))
            # ignore files in the test data .git directory
            if (
                # ignore .git
                os.path.relpath(data_file, data_dir).split(os.sep, 1)[0]
                != ".git"
            )
        }

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=int(
                os.environ.get(
                    "IBIS_DATA_MAX_WORKERS",
                    URLLIB_DEFAULT_POOL_SIZE,
                )
            )
        )

        hdfs = con.hdfs
        tasks = {
            # make the database
            executor.submit(impala_create_test_database, con, env),
            # build and upload UDFs
            *itertools.starmap(
                executor.submit,
                impala_build_and_upload_udfs(hdfs, env, fs=fs),
            ),
            # upload data files
            *(
                executor.submit(
                    hdfs_make_dir_and_put_file,
                    hdfs,
                    data_file,
                    os.path.join(
                        env.test_data_dir,
                        os.path.relpath(data_file, data_dir),
                    ),
                )
                for data_file in data_files
            ),
        }

        for future in concurrent.futures.as_completed(tasks):
            future.result()

        # create the tables and compute stats
        for future in concurrent.futures.as_completed(
            executor.submit(table_future.result().compute_stats)
            for table_future in concurrent.futures.as_completed(
                impala_create_tables(con, env, executor=executor)
            )
        ):
            future.result()

    @staticmethod
    def connect(data_directory: Path):
        fsspec = pytest.importorskip("fsspec")

        env = IbisTestEnv()
        return ibis.impala.connect(
            host=env.impala_host,
            port=env.impala_port,
            auth_mechanism=env.auth_mechanism,
            hdfs_client=fsspec.filesystem(
                env.hdfs_protocol,
                host=env.nn_host,
                port=env.hdfs_port,
                user=env.hdfs_user,
            ),
            database=env.test_data_db,
        )

    def _get_original_column_names(self, tablename: str) -> list[str]:
        import pyarrow.parquet as pq

        pq_file = pq.ParquetFile(
            self.data_directory
            / "parquet"
            / tablename
            / f"{tablename}.parquet"
        )
        return pq_file.schema.names

    def _get_renamed_table(self, tablename: str) -> ir.Table:
        t = self.connection.table(tablename)
        original_names = self._get_original_column_names(tablename)
        return t.relabel(dict(zip(t.columns, original_names)))

    @property
    def batting(self) -> ir.Table:
        return self._get_renamed_table("batting")

    @property
    def awards_players(self) -> ir.Table:
        return self._get_renamed_table("awards_players")


class IbisTestEnv:
    def __init__(self):
        if options.impala is None:
            ibis.backends.impala.Backend.register_options()

    @property
    def impala_host(self):
        return os.environ.get('IBIS_TEST_IMPALA_HOST', 'localhost')

    @property
    def impala_port(self):
        return int(os.environ.get('IBIS_TEST_IMPALA_PORT', "21050"))

    @property
    def tmp_db(self):
        options.impala.temp_db = tmp_db = os.environ.get(
            'IBIS_TEST_TMP_DB', 'ibis_testing_tmp_db'
        )
        return tmp_db

    @property
    def tmp_dir(self):
        options.impala.temp_hdfs_path = tmp_dir = os.environ.get(
            'IBIS_TEST_TMP_HDFS_DIR', f'/tmp/__ibis_test_{util.guid()}'
        )
        return tmp_dir

    @property
    def test_data_db(self):
        return os.environ.get('IBIS_TEST_DATA_DB', 'ibis_testing')

    @property
    def test_data_dir(self):
        return os.environ.get(
            'IBIS_TEST_DATA_HDFS_DIR', '/__ibis/ibis-testing-data'
        )

    @property
    def nn_host(self):
        return os.environ.get('IBIS_TEST_NN_HOST', 'localhost')

    @property
    def hdfs_port(self):
        return int(os.environ.get('IBIS_TEST_HDFS_PORT', 50070))

    @property
    def hdfs_superuser(self):
        return os.environ.get('IBIS_TEST_HDFS_SUPERUSER', 'hdfs')

    @property
    def use_codegen(self):
        return (
            os.environ.get('IBIS_TEST_USE_CODEGEN', 'False').lower() == 'true'
        )

    @property
    def auth_mechanism(self):
        return os.environ.get('IBIS_TEST_AUTH_MECH', 'NOSASL')

    @property
    def hdfs_user(self):
        return os.environ.get('IBIS_TEST_HDFS_USER', 'hdfs')

    @property
    def hdfs_protocol(self):
        return os.environ.get("IBIS_TEST_HDFS_PROTOCOL", "webhdfs")


@pytest.fixture
def env():
    return IbisTestEnv()


@pytest.fixture
def tmp_dir(env):
    options.impala.temp_hdfs_path = tmp_dir = env.tmp_dir
    return tmp_dir


@pytest.fixture
def test_data_db(env):
    return env.test_data_db


@pytest.fixture
def test_data_dir(env):
    return env.test_data_dir


@pytest.fixture
def hdfs(env, tmp_dir):
    fsspec = pytest.importorskip("fsspec")
    client = fsspec.filesystem(
        env.hdfs_protocol,
        host=env.nn_host,
        port=env.hdfs_port,
        user=env.hdfs_user,
    )

    if not client.exists(tmp_dir):
        client.mkdir(tmp_dir)
    return client


@pytest.fixture
def con_no_hdfs(
    env,
    test_data_db,
    tmp_path_factory,
    data_directory,
    script_directory,
):
    con = lock_load_data(
        TestConf,
        tmp_path_factory,
        data_directory,
        script_directory,
        with_hdfs=False,
        database=test_data_db,
    )
    if not env.use_codegen:
        con.disable_codegen()
    assert con.get_options()['DISABLE_CODEGEN'] == '1'
    try:
        yield con
    finally:
        con.set_database(test_data_db)


@pytest.fixture
def con(
    env,
    test_data_db,
    tmp_path_factory,
    data_directory,
    script_directory,
):
    con = lock_load_data(
        TestConf,
        tmp_path_factory,
        data_directory,
        script_directory,
        database=test_data_db,
    )
    if not env.use_codegen:
        con.disable_codegen()
    assert con.get_options()['DISABLE_CODEGEN'] == '1'
    try:
        yield con
    finally:
        con.set_database(test_data_db)


@pytest.fixture
def temp_char_table(con):
    statement = """\
CREATE TABLE IF NOT EXISTS {} (
  `group1` varchar(10),
  `group2` char(10)
)"""
    name = 'testing_varchar_support'
    sql = statement.format(name)
    con.con.execute(sql)
    try:
        yield con.table(name)
    finally:
        assert name in con.list_tables(), name
        con.drop_table(name)


@pytest.fixture
def tmp_db(env, con, test_data_db):
    impala = pytest.importorskip("impala")

    tmp_db = env.tmp_db

    if tmp_db not in con.list_databases():
        con.create_database(tmp_db)
    try:
        yield tmp_db
    finally:
        con.set_database(test_data_db)
        try:
            con.drop_database(tmp_db, force=True)
        except impala.error.HiveServer2Error:
            # The database can be dropped by another process during tear down
            # in the middle of dropping this one if tests are running in
            # parallel.
            #
            # We only care that it gets dropped before all tests are finished
            # running.
            pass


@pytest.fixture
def con_no_db(env, tmp_path_factory, data_directory, script_directory):
    con = lock_load_data(
        TestConf,
        tmp_path_factory,
        data_directory,
        script_directory,
    )
    if not env.use_codegen:
        con.disable_codegen()
    assert con.get_options()['DISABLE_CODEGEN'] == '1'
    try:
        yield con
    finally:
        con.set_database(None)


@pytest.fixture
def alltypes(con):
    return con.table("functional_alltypes")


@pytest.fixture
def alltypes_df(alltypes):
    return alltypes.execute()


def _random_identifier(suffix):
    return f'__ibis_test_{suffix}_{util.guid()}'


@pytest.fixture
def temp_database(con, test_data_db):
    name = _random_identifier('database')
    con.create_database(name)
    try:
        yield name
    finally:
        con.set_database(test_data_db)
        con.drop_database(name, force=True)


@pytest.fixture
def temp_table(con):
    name = _random_identifier('table')
    try:
        yield name
    finally:
        assert name in con.list_tables(), name
        con.drop_table(name)


@pytest.fixture
def temp_table_db(con, temp_database):
    name = _random_identifier('table')
    try:
        yield temp_database, name
    finally:
        assert name in con.list_tables(database=temp_database), name
        con.drop_table(name, database=temp_database)


@pytest.fixture
def temp_view(con):
    name = _random_identifier('view')
    try:
        yield name
    finally:
        assert name in con.list_tables(), name
        con.drop_view(name)


@pytest.fixture
def temp_parquet_table_schema():
    return ibis.schema(
        [('id', 'int32'), ('name', 'string'), ('files', 'int32')]
    )


@pytest.fixture
def temp_parquet_table(con, tmp_db, temp_parquet_table_schema):
    name = util.guid()
    db = con.database(tmp_db)
    db.create_table(name, schema=temp_parquet_table_schema, format='parquet')
    try:
        yield db[name]
    finally:
        db.client.drop_table(name, database=tmp_db)


@pytest.fixture
def temp_parquet_table2(con, tmp_db, temp_parquet_table_schema):
    name = util.guid()
    db = con.database(tmp_db)
    db.create_table(name, schema=temp_parquet_table_schema, format='parquet')
    try:
        yield db[name]
    finally:
        db.client.drop_table(name, database=tmp_db)


@pytest.fixture(scope="session")
def mockcon():
    return MockBackend()


@pytest.fixture
def kudu_table(con, test_data_db):
    name = 'kudu_backed_table'
    con.raw_sql(
        f"""
CREATE TABLE {test_data_db}.{name} (
  a STRING,
  PRIMARY KEY(a)
)
PARTITION BY HASH PARTITIONS 2
STORED AS KUDU
TBLPROPERTIES (
  'kudu.master_addresses' = 'kudu',
  'kudu.num_tablet_replicas' = '1'
)"""
    )
    drop_sql = f'DROP TABLE {test_data_db}.{name}'
    try:
        yield con.table(name)
    finally:
        con.raw_sql(drop_sql)


def translate(expr, context=None, named=False):
    if context is None:
        context = ImpalaCompiler.make_context()
    translator = ImpalaExprTranslator(expr, context=context, named=named)
    return translator.get_result()


def impala_build_and_upload_udfs(hdfs, env, *, fs):
    IBIS_HOME = Path(__file__).absolute().parents[4]
    cwd = str(IBIS_HOME / "ci" / "udf")
    subprocess.run(["cmake", ".", "-G", "Ninja"], cwd=cwd)
    subprocess.run(["ninja"], cwd=cwd)
    build_dir = IBIS_HOME / "ci" / "udf" / "build"
    bitcode_dir = os.path.join(env.test_data_dir, "udf")

    hdfs.mkdir(bitcode_dir, create_parents=True)

    for file in fs.find(build_dir):
        bitcode_path = os.path.join(
            bitcode_dir, os.path.relpath(file, build_dir)
        )
        yield hdfs.put_file, file, bitcode_path


def hdfs_make_dir_and_put_file(fs, src, target):
    fs.mkdir(os.path.dirname(target), create_parents=True)
    fs.put_file(src, target)


def impala_create_test_database(con, env):
    con.drop_database(env.test_data_db, force=True)
    con.create_database(env.test_data_db)
    con.create_table(
        'alltypes',
        schema=ibis.schema(
            [
                ('a', 'int8'),
                ('b', 'int16'),
                ('c', 'int32'),
                ('d', 'int64'),
                ('e', 'float'),
                ('f', 'double'),
                ('g', 'string'),
                ('h', 'boolean'),
                ('i', 'timestamp'),
            ]
        ),
        database=env.test_data_db,
    )


PARQUET_SCHEMAS = {
    'functional_alltypes': TEST_TABLES["functional_alltypes"].delete(
        ["index", "Unnamed: 0"]
    ),
    "tpch_region": ibis.schema(
        [
            ("r_regionkey", "int16"),
            ("r_name", "string"),
            ("r_comment", "string"),
        ]
    ),
}

PARQUET_SCHEMAS.update(
    (table, schema)
    for table, schema in TEST_TABLES.items()
    if table != "functional_alltypes"
)

AVRO_SCHEMAS = {
    "tpch_region_avro": {
        "type": "record",
        "name": "a",
        "fields": [
            {"name": "R_REGIONKEY", "type": ["null", "int"]},
            {"name": "R_NAME", "type": ["null", "string"]},
            {"name": "R_COMMENT", "type": ["null", "string"]},
        ],
    }
}

ALL_SCHEMAS = collections.ChainMap(PARQUET_SCHEMAS, AVRO_SCHEMAS)


def impala_create_tables(con, env, *, executor=None):
    test_data_dir = env.test_data_dir
    avro_files = [
        (con.avro_file, os.path.join(test_data_dir, 'avro', path))
        for path in con.hdfs.ls(os.path.join(test_data_dir, 'avro'))
    ]
    parquet_files = [
        (con.parquet_file, os.path.join(test_data_dir, 'parquet', path))
        for path in con.hdfs.ls(os.path.join(test_data_dir, 'parquet'))
    ]
    for method, path in itertools.chain(parquet_files, avro_files):
        yield executor.submit(
            method,
            path,
            ALL_SCHEMAS.get(os.path.basename(path)),
            name=os.path.basename(path),
            database=env.test_data_db,
            persist=True,
        )
