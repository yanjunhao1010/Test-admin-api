from fastapi import APIRouter, File, UploadFile, HTTPException
import pandas as pd
import numpy as np
import io
import os
from typing import Dict, Any, List
from src.comment import MySqlconection,yaml_data

router = APIRouter(
    prefix='/upload',
    tags=['upload'])


# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


def get_db_handler():
    """数据库连接依赖注入"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(data['mysql']['host'],data['mysql']['user'], data['mysql']['password'], data['mysql']['database'])
    return db


def safe_convert_dataframe(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    安全地将 DataFrame 转换为 JSON 兼容的列表字典
    """
    result = []

    for _, row in df.iterrows():
        record = {}
        for col in df.columns:
            value = row[col]

            # 处理空值
            if pd.isna(value):
                record[col] = None
                continue

            # 处理 numpy 数值类型
            if isinstance(value, np.generic):
                if isinstance(value, np.floating):
                    if np.isnan(value) or np.isinf(value):
                        record[col] = None
                    else:
                        record[col] = float(value)
                elif isinstance(value, np.integer):
                    record[col] = int(value)
                elif isinstance(value, np.bool_):
                    record[col] = bool(value)
                else:
                    try:
                        record[col] = value.item()
                    except:
                        record[col] = str(value)
                continue

            # 处理 pandas 时间戳
            if isinstance(value, pd.Timestamp):
                record[col] = value.isoformat()
                continue

            # 默认情况
            record[col] = value

        result.append(record)

    return result


def build_column_map(columns) -> dict:
    """
    将 Excel 列头映射到标准数据库字段，兼容多种模板写法
    （如：项目 / 项目名称、模块 / 所属模块、优先级 各种写法等）
    """
    aliases = {
        'project_id': ['项目名称', '项目', 'project', 'projectid', 'project_id'],
        'module': ['所属模块', '模块', 'module'],
        'function_point': ['测试功能点', '功能点', '测试功能', 'functionpoint', 'function_point'],
        'pre_condition': ['前置条件', 'precondition', 'pre_condition'],
        'steps': ['步骤', 'steps'],
        'expected_result': ['预期结果', 'expectedresult', 'expected_result'],
        'remark': ['备注', 'remark'],
    }
    col_map = {}
    for col in columns:
        n = str(col).replace('\n', '').replace(' ', '').replace('（', '(').replace('）', ')').lower()
        # 优先级表头写法多变，只要含「优先级」即命中
        if '优先级' in n:
            col_map.setdefault('priority', col)
            continue
        for field, al in aliases.items():
            if n in al or any(a.replace(' ', '').lower() in n for a in al):
                col_map.setdefault(field, col)
                break
    return col_map


def normalize_priority(value):
    """
    把优先级统一成 1/2/3：
        高 -> 1, 中 -> 2, 低 -> 3
        P1/P2/P3 -> 1/2/3
        直接写数字 1/2/3 原样返回
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 2
    s = str(value).strip().upper()
    mapping = {
        'P1': 1, 'P2': 2, 'P3': 3,
        '1': 1, '2': 2, '3': 3,
        '高': 1, '中': 2, '低': 3,
        '高优先级': 1, '中优先级': 2, '低优先级': 3,
    }
    if s in mapping:
        return mapping[s]
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 2


@router.post("/upload_file")
async def upload_excel_safe(
        file: UploadFile = File(...),
        sheet_name: str = None,
        na_values: List[str] = None
):
    """
    安全地上传和处理 Excel 文件
    """
    try:
        # 验证文件类型
        if not file.filename.lower().endswith(('.xlsx', '.xls', '.xlsm')):
            raise HTTPException(400, "仅支持 Excel 文件")

        # 读取文件
        contents = await file.read()

        # 读取 Excel，指定空值
        if na_values is None:
            na_values = ['', 'NA', 'N/A', 'NaN', 'null', 'NULL']

        # 读取所有工作表（sheet_name=None 返回 {表名: DataFrame} 字典），
        # 只保留有数据的表并纵向拼接，实现「有几张有数据的表就导入几张」
        raw = pd.read_excel(
            io.BytesIO(contents),
            sheet_name=None,
            na_values=na_values,
            keep_default_na=True,
        )
        if isinstance(raw, dict):
            df = pd.concat([v for v in raw.values() if not v.empty], ignore_index=True)
        else:
            df = raw

        if df.empty or len(df.columns) == 0:
            return {
                "code": 200,
                "msg": "success",
                "total": 0,
                "data": {"新增0条用例成功"},
            }

        # 将 Excel 列头映射到数据库字段（兼容不同模板表头）
        col_map = build_column_map(df.columns)
        missing = [
            f for f in ('project_id', 'module', 'function_point', 'steps', 'expected_result')
            if f not in col_map
        ]
        if missing:
            raise HTTPException(status_code=400, detail=f"Excel 缺少必要列，请检查模板：{', '.join(missing)}")

        records = safe_convert_dataframe(df)

        try:
            with get_db_handler() as db:
                # 一次性取出 项目名称 -> id 映射，用于把「项目名称」转换成 project_id
                proj_rows = db.execute_query(
                    "SELECT id, cate_name FROM test_case_project WHERE is_del = 0"
                )
                proj_map = {str(r['cate_name']).strip(): r['id'] for r in proj_rows}

                insert_sql = """
                        INSERT INTO test_cases (
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

                success_count = 0
                for idx, rec in enumerate(records, start=2):  # Excel 数据从第 2 行开始
                    # 项目名称 -> project_id（兼容直接填数字 id 的旧模板）
                    proj_raw = rec.get(col_map['project_id'])
                    pid = None
                    if proj_raw is not None:
                        s = str(proj_raw).strip()
                        if s.isdigit():
                            pid = int(s)
                        else:
                            # 兼容 Excel 把整数项目号读成浮点（如 11.0）的情况
                            try:
                                fv = float(s)
                                if fv.is_integer():
                                    pid = int(fv)
                            except (ValueError, TypeError):
                                pass
                            if pid is None:
                                pid = proj_map.get(s)
                    if pid is None:
                        raise HTTPException(
                            status_code=400,
                            detail=f"第{idx}行项目名称「{proj_raw}」在项目中不存在，请先在项目管理中维护",
                        )

                    params = (
                        pid,
                        rec.get(col_map['module']),
                        rec.get(col_map['function_point']),
                        normalize_priority(rec.get(col_map['priority'])),
                        rec.get(col_map['pre_condition']),
                        rec.get(col_map['steps']),
                        rec.get(col_map['expected_result']),
                        rec.get(col_map['remark']),
                        1,  # status
                        0,  # is_del
                    )
                    db.execute_update(insert_sql, params)
                    success_count += 1

                return {
                    "code": 200,
                    "msg": "success",
                    "total": success_count,
                    "data": {
                        f"新增{success_count}条用例成功"
                    },
                }
        except HTTPException:
            raise
        except Exception as e:
            print(f"错误详情: {str(e)}")
            raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")

    except pd.errors.EmptyDataError:
        raise HTTPException(400, "Excel 文件为空")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"处理失败: {str(e)}")