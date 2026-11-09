from openpyxl import load_workbook




def read_excel_openpyxl(file_path):
    """使用openpyxl读取Excel"""
    try:
        wb = load_workbook(file_path, data_only=True)

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            print(f"\n工作表: {sheet_name}")
            print(f"总行数: {ws.max_row}, 总列数: {ws.max_column}")

            # 读取表头
            headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
            print(f"表头: {headers}")

            # 读取前5行数据
            data = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                data.append(row)
            for row in data:
                print(row)

        return wb
    except Exception as e:
        print(f"读取失败: {e}")
        return None


# 使用示例
if __name__ == "__main__":
    file_path = "D:\\文档\\talkMi文档\\小程序V1.2测试上传.xlsx"
    # 使用openpyxl
    print("\n=== 使用openpyxl读取 ===")
    data2 = read_excel_openpyxl(file_path)
    for row in data2:
        print(row)