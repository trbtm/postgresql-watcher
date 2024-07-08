from typing import Optional, Callable
from psycopg2 import connect, extensions
from multiprocessing import Process, Pipe
from multiprocessing.connection import Connection
import time
from select import select
from logging import Logger, getLogger


POSTGRESQL_CHANNEL_NAME = "casbin_role_watcher"
CASBIN_CHANNEL_SELECT_TIMEOUT = 1 # seconds


class PostgresqlWatcher(object):

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 5432,
        dbname: str = "postgres",
        channel_name: Optional[str] = None,
        start_listening: bool = True,
        sslmode: Optional[str] = None,
        sslrootcert: Optional[str] = None,
        sslcert: Optional[str] = None,
        sslkey: Optional[str] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        """
        Initialize a PostgresqlWatcher object.

        Args:
            host (str): Hostname of the PostgreSQL server.
            user (str): PostgreSQL username.
            password (str): Password for the user.
            port (int): Post of the PostgreSQL server. Defaults to 5432.
            dbname (str): Database name. Defaults to "postgres".
            channel_name (str): The name of the channel to listen to and to send updates to. When None a default is used.
            start_listening (bool, optional): Flag whether to start listening to updates on the PostgreSQL channel. Defaults to True.
            sslmode (Optional[str], optional): See `psycopg2.connect` for details. Defaults to None.
            sslrootcert (Optional[str], optional): See `psycopg2.connect` for details. Defaults to None.
            sslcert (Optional[str], optional): See `psycopg2.connect` for details. Defaults to None.
            sslkey (Optional[str], optional): See `psycopg2.connect` for details. Defaults to None.
            logger (Optional[Logger], optional): Custom logger to use. Defaults to None.
        """
        self.update_callback = None
        self.parent_conn = None
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.dbname = dbname
        self.channel_name = channel_name if channel_name is not None else POSTGRESQL_CHANNEL_NAME
        self.sslmode = sslmode
        self.sslrootcert = sslrootcert
        self.sslcert = sslcert
        self.sslkey = sslkey
        if logger is None:
            logger = getLogger()
        self.logger = logger
        self.parent_conn: Connection | None = None
        self.child_conn: Connection | None = None
        self.subscription_process: Process | None = None
        self._create_subscription_process(start_listening)
        self.update_callback: Optional[Callable] = None

    def __del__(self) -> None:
        self._cleanup_connections_and_processes()

    def _create_subscription_process(
        self,
        start_listening=True,
        delay: Optional[int] = 2,
    ) -> None:
        self._cleanup_connections_and_processes()

        self.parent_conn, self.child_conn = Pipe()
        self.subscribed_process = Process(
            target=_casbin_channel_subscription,
            args=(
                self.child_conn,
                self.logger,
                self.host,
                self.user,
                self.password,
                self.port,
                self.dbname,
                delay,
                self.channel_name,
                self.sslmode,
                self.sslrootcert,
                self.sslcert,
                self.sslkey,
            ),
            daemon=True,
        )
        if start_listening:
            self.subscribed_process.start()

    def _cleanup_connections_and_processes(self) -> None:
        # Clean up potentially existing Connections and Processes
        if self.parent_conn is not None:
            self.parent_conn.close()
            self.parent_conn = None
        if self.child_conn is not None:
            self.child_conn.close()
            self.child_conn = None
        if self.subscription_process is not None:
            self.subscription_process.terminate()
            self.subscription_process = None

    def set_update_callback(self, update_handler: Callable):
        """
        Set the handler called, when the Watcher detects an update.
        Recommendation: `casbin_enforcer.adapter.load_policy`
        """
        self.update_callback = update_handler

    def update(self) -> None:
        """
        Called by `casbin.Enforcer` when an update to the model was made.
        Informs other watchers via the PostgreSQL channel.
        """
        conn = connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=self.dbname,
            sslmode=self.sslmode,
            sslrootcert=self.sslrootcert,
            sslcert=self.sslcert,
            sslkey=self.sslkey,
        )
        # Can only receive notifications when not in transaction, set this for easier usage
        conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        curs = conn.cursor()
        curs.execute(
            f"NOTIFY {self.channel_name},'casbin policy update at {time.time()}'"
        )
        conn.close()

    def should_reload(self) -> bool:
        try:
            if self.parent_conn.poll():
                message = self.parent_conn.recv()
                self.logger.debug(f"message:{message}")
                return True
        except EOFError:
            self.logger.warning(
                "Child casbin-watcher subscribe process has stopped, "
                "attempting to recreate the process in 10 seconds..."
            )
            self._create_subscription_process(delay=10)

        return False


def _casbin_channel_subscription(
    process_conn: Connection,
    logger: Logger,
    host: str,
    user: str,
    password: str,
    port: Optional[int] = 5432,
    dbname: Optional[str] = "postgres",
    delay: Optional[int] = 2,
    channel_name: Optional[str] = POSTGRESQL_CHANNEL_NAME,
    sslmode: Optional[str] = None,
    sslrootcert: Optional[str] = None,
    sslcert: Optional[str] = None,
    sslkey: Optional[str] = None,
):
    # delay connecting to postgresql (postgresql connection failure)
    time.sleep(delay)
    conn = connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        sslmode=sslmode,
        sslrootcert=sslrootcert,
        sslcert=sslcert,
        sslkey=sslkey,
    )
    # Can only receive notifications when not in transaction, set this for easier usage
    conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    curs = conn.cursor()
    curs.execute(f"LISTEN {channel_name};")
    logger.debug("Waiting for casbin policy update")
    while not curs.closed:
        if not select([conn], [], [], CASBIN_CHANNEL_SELECT_TIMEOUT) == ([], [], []):
            logger.debug("Casbin policy update identified..")
            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop(0)
                logger.debug(f"Notify: {notify.payload}")
                process_conn.send(notify.payload)
