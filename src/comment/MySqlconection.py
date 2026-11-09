import json
import queue
import threading
import time
from itertools import count

import pymysql
import logging
from typing import List, Dict, Any, Optional, Union
from src.comment.yaml_data import BaseYamlData
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
import cryptography


# ---------- 连接池 ----------
# 背景：MySQL 走公网（8.134.96.9），每个请求新建连接 = 一次公网 TCP 握手（~100-300ms），
# 且偶发 connect timeout (2003)。改为「取用/归还」模式后，现有 with db: 代码零改动即受益。

DEFAULT_POOL_SIZE = 10
CONNECT_RETRY = 3                    # 建连失败重试次数
CONNECT_BACKOFF = (0.3, 0.9, 2.5)    # 退避间隔（秒）


class ConnectionPool:
    """线程安全的 MySQL 连接池。取用时 ping 检查死连接，归还时回滚未提交事务。"""

    def __init__(self, maxsize: int = DEFAULT_POOL_SIZE):
        self._queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()

    def acquire(self, db_config: dict, retry: int = CONNECT_RETRY,
                backoff: tuple = CONNECT_BACKOFF):
        """取一条可用连接：先从池里拿（ping 检查），没有就新建（带重试退避）。"""
        # 1) 尝试从池中取
        while True:
            try:
                conn = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                continue  # 死连接，丢掉继续拿下一条

        # 2) 新建连接（重试 + 指数退避）
        last_err = None
        for attempt in range(1, retry + 1):
            try:
                return pymysql.connect(**db_config)
            except pymysql.MySQLError as e:
                last_err = e
                logger.warning(f"数据库连接失败（第 {attempt}/{retry} 次）: {e}")
                if attempt < retry:
                    time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
        raise last_err

    def release(self, conn) -> None:
        """归还连接：回滚残留事务后放回池；池满或连接已坏则直接关闭。"""
        if conn is None:
            return
        try:
            if not conn.open:
                return
            conn.rollback()  # 清掉未提交事务，避免污染下一个使用者
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return
        try:
            self._queue.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass


_POOL = ConnectionPool()


def load_config_cached(path: str) -> dict:
    """读取 YAML 配置并按路径缓存（每个请求都读盘+解析是纯浪费）。"""
    cache = getattr(load_config_cached, '_cache', None)
    if cache is None:
        cache = {}
        load_config_cached._cache = cache
    cfg = cache.get(path)
    if cfg is None:
        cfg = BaseYamlData(path).read_yaml()
        cache[path] = cfg
    return cfg


class DatabaseHandler:

    def __init__(self, host: str, user: str, password: str, database: str,
                 port: int = 3306, charset: str = 'utf8mb4',
                 autocommit: bool = False, use_pool: bool = True,
                 connect_timeout: int = 3, read_timeout: int = 30):
        """
        初始化数据库连接

        Args:
            host: 数据库主机
            user: 用户名
            password: 密码
            database: 数据库名
            port: 端口，默认3306
            charset: 字符集，默认utf8mb4
            autocommit: 是否自动提交，默认False
            use_pool: 是否走连接池（默认True，关闭则每次新建/真关）
            connect_timeout: 建连超时秒数，默认3（公网 MySQL 正常建连约 50ms；
                3s×3次重试+退避≈12.7s，仍在前端 axios 15s 超时内，避免前端先超时）
            read_timeout: 读超时秒数，默认30
        """
        self.db_config = {
            'host': host,
            'user': user,
            'password': password,
            'database': database,
            'port': port,
            'charset': charset,
            'autocommit': autocommit,
            'cursorclass': pymysql.cursors.DictCursor,
            'connect_timeout': connect_timeout,
            'read_timeout': read_timeout,
        }
        self.connection = None
        self.use_pool = use_pool
        self._owns_connection = False   # True=自己新建的，归还时要真关

    def __enter__(self):
        """支持 with 语句"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出 with 语句时自动关闭连接"""
        self.close()

    def connect(self):
        """建立数据库连接（走连接池，建连失败自动重试退避）"""
        try:
            if self.use_pool:
                self.connection = _POOL.acquire(self.db_config)
                self._owns_connection = False
            else:
                self.connection = pymysql.connect(**self.db_config)
                self._owns_connection = True
            logger.info("数据库连接成功")
        except pymysql.MySQLError as e:
            logger.error(f"数据库连接失败（已重试 {CONNECT_RETRY} 次）: {e}")
            raise

    def close(self):
        """关闭/归还连接"""
        if self.connection and self.connection.open:
            if self.use_pool and not self._owns_connection:
                _POOL.release(self.connection)
                logger.info("数据库连接已归还连接池")
            else:
                self.connection.close()
                logger.info("数据库连接已关闭")
        self.connection = None

    def _ensure_connection(self):
        """确保连接有效"""
        if not self.connection or not self.connection.open:
            self.connect()

    def execute_query(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """
        执行查询语句（SELECT）

        Args:
            sql: SQL语句
            params: 参数元组

        Returns:
            查询结果列表
        """
        self._ensure_connection()

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params or ())
                result = cursor.fetchall()
                logger.debug(f"查询执行成功，影响行数: {cursor.rowcount}")
                return result
        except pymysql.MySQLError as e:
            logger.error(f"查询执行失败: {e}, SQL: {sql}")
            raise

    def execute_single_query(self, sql: str, params: Optional[tuple] = None) -> Optional[Dict[str, Any]]:
        """
        执行查询并返回单条结果

        Args:
            sql: SQL语句
            params: 参数元组

        Returns:
            单条查询结果或None
        """
        self._ensure_connection()

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params or ())
                result = cursor.fetchone()
                return result
        except pymysql.MySQLError as e:
            logger.error(f"单条查询执行失败: {e}, SQL: {sql}")
            raise

    def execute_update(self, sql: str, params: Optional[tuple] = None) -> int:
        """
        执行更新操作（INSERT, UPDATE, DELETE）

        Args:
            sql: SQL语句
            params: 参数元组

        Returns:
            影响的行数
        """
        self._ensure_connection()

        try:
            with self.connection.cursor() as cursor:
                affected_rows = cursor.execute(sql, params or ())
                self.connection.commit()
                logger.debug(f"更新执行成功，影响行数: {affected_rows}")
                return affected_rows
        except pymysql.MySQLError as e:
            self.connection.rollback()
            logger.error(f"更新执行失败: {e}, SQL: {sql}")
            raise

    def execute_many(self, sql: str, params_list: List[tuple]) -> int:
        """
        批量执行操作

        Args:
            sql: SQL语句
            params_list: 参数列表

        Returns:
            影响的总行数
        """
        self._ensure_connection()

        try:
            with self.connection.cursor() as cursor:
                affected_rows = cursor.executemany(sql, params_list)
                self.connection.commit()
                logger.debug(f"批量执行成功，影响行数: {affected_rows}")
                return affected_rows
        except pymysql.MySQLError as e:
            self.connection.rollback()
            logger.error(f"批量执行失败: {e}, SQL: {sql}")
            raise

if __name__ == "__main__":
    data = BaseYamlData("D:\\pyproject\\Fast_api_project\\src\\config\\config.yml").read_yaml()
    host = data['mysql']['host']
    user = data['mysql']['user']
    password = data['mysql']['password']
    database = data['mysql']['database']
    charset = data['mysql']['charset']
    port = data['mysql']['port']
    test_case_data=[
        {'用例编号': 1, '项目': 11, '所属模块': '商家详情页数据显示', '测试功能点': '商家详情-车主评价-图片展示', '优先级（1:高/2:中/3:低/）': 1, '前置条件': '有用户评论，评论有五张图片', '步骤': '无', '预期结果': '点击评论图片，能全屏查看，点击退出查看，则返回评论页', '备注': None},
        {'用例编号': 2, '项目': 11, '所属模块': '商家详情页数据显示', '测试功能点': '商家详情-车主评价-图片展示', '优先级（1:高/2:中/3:低/）': 1, '前置条件': '有用户评论，评论有六张图片', '步骤': '无', '预期结果': '图片显示在用户评论下部，最多只显示前三张小图', '备注': None}]
    try:
        with DatabaseHandler(host, user, password, database) as db:
            query = """
                  INSERT INTO zentao.test_cases (
                    project_id, 
                    module, 
                    function_point, 
                    priority, 
                    pre_condition, 
                    steps, 
                    expected_result, 
                    remark, 
                    status, 
                    is_del
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)  
            """
            for test_case in test_case_data:
                params = (
                    test_case['项目'],
                    test_case['所属模块'],
                    test_case['测试功能点'],
                    test_case['优先级（1:高/2:中/3:低/）'],
                    test_case['前置条件'],
                    test_case['步骤'],
                    test_case['预期结果'],
                    test_case['备注'],
                    1,  # status
                    0,  # is_del
                )
                db_result=db.execute_update(query,params)
                print(db_result)
    except pymysql.MySQLError as e:
        logger.error(e)
