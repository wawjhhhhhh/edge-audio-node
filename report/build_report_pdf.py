# -*- coding: utf-8 -*-
"""把 design_report.docx 转成 PDF（走 WPS 的 COM 接口，环境里没有 LibreOffice/pandoc）。

同时处理一个现实问题：如果 WPS 里正开着旧版 design_report.docx，会锁住文件，
导致新文件无法替换。本脚本会先关掉那份旧文档（不保存），再替换、再导出 PDF。

用法：python build_report_pdf.py
"""
import os
import sys
import time
import shutil

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
DOCX = os.path.join(HERE, "design_report.docx")
NEW = os.path.join(HERE, "design_report_v3.docx")
PDF = os.path.join(HERE, "design_report.pdf")
SRC = NEW if os.path.isfile(NEW) else DOCX

import comtypes.client  # noqa: E402


def main():
    if not os.path.isfile(SRC):
        raise SystemExit("没有可导出的 docx：%s" % SRC)
    print("源文件：%s（%d 字节）" % (SRC, os.path.getsize(SRC)))

    app = comtypes.client.CreateObject("KWPS.Application")
    try:
        app.Visible = False
    except Exception:
        pass
    try:
        app.DisplayAlerts = 0
    except Exception:
        pass

    # 1) 关掉锁住设计文档的旧窗口（只关这一个文件，不动用户其他文档）
    closed = []
    try:
        for i in range(app.Documents.Count, 0, -1):
            d = app.Documents.Item(i)
            try:
                full = str(d.FullName)
            except Exception:
                continue
            if os.path.basename(full).lower() == "design_report.docx":
                d.Close(0)
                closed.append(full)
    except Exception as e:
        print("枚举文档失败（忽略）：%s" % e)
    if closed:
        print("已关闭占用中的旧文档：%s" % closed)
        time.sleep(1.0)

    # 2) 替换主文件
    if SRC != DOCX:
        for attempt in range(20):
            try:
                if os.path.exists(DOCX):
                    os.remove(DOCX)
                shutil.move(SRC, DOCX)
                print("已替换主文件 design_report.docx")
                break
            except PermissionError as e:
                if attempt == 19:
                    print("主文件仍被占用，仅导出 v3：%s" % e)
                    break
                time.sleep(1.0)

    target_docx = SRC  # 优先导出刚生成的那一份（v3 存在时就是 v3）

    # 3) 导出 PDF：先写临时文件，再原子替换（直接覆盖旧 PDF 时 WPS 可能静默失败）
    tmp_pdf = os.path.join(HERE, "_export_tmp.pdf")
    if os.path.exists(tmp_pdf):
        os.remove(tmp_pdf)
    doc = app.Documents.Open(target_docx)
    doc.SaveAs(tmp_pdf, 17)  # 17 = wdFormatPDF
    pages = None
    try:
        pages = doc.ComputeStatistics(2)  # 2 = wdStatisticPages
    except Exception:
        pass
    doc.Close(0)
    for attempt in range(15):
        try:
            os.replace(tmp_pdf, PDF)
            break
        except PermissionError:
            if attempt == 14:
                print("PDF 被占用，保留临时文件：%s" % tmp_pdf)
                break
            time.sleep(1.0)
    print("已生成 PDF：%s（%d 字节）%s" % (PDF, os.path.getsize(PDF),
                                        "，%s 页" % pages if pages else ""))

    try:
        app.Quit()
    except Exception:
        pass


if __name__ == "__main__":
    main()
