"""엑셀 생성 – DB 레코드 기반"""

import io, os, math
import glob as _glob
from datetime import date, timedelta
from PIL import Image

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image as XLImage
from openpyxl.worksheet.page import PageMargins
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import CellIsRule
from openpyxl.drawing.spreadsheet_drawing import TwoCellAnchor, AnchorMarker

DAYS_KO = ["월","화","수","목","금","토","일"]
SLOTS   = ["오전1","오전2","오후1","오후2"]


def _excel_font() -> str:
    dirs = (
        [r"C:\Windows\Fonts"] if os.name == "nt"
        else ["/usr/share/fonts","/usr/local/share/fonts",
              os.path.expanduser("~/.local/share/fonts")]
    )
    for d in dirs:
        if not os.path.isdir(d): continue
        # 나눔스퀘어 우선 탐색
        for p in ["*NanumSquare*","*나눔스퀘어*","NanumSquare*.ttf","NanumSquare*.otf"]:
            hits = _glob.glob(os.path.join(d,"**",p),recursive=True) or _glob.glob(os.path.join(d,p))
            if hits: return "NanumSquare"
        # 현대하모니 폴백
        for p in ["*[Hh]yundai*[Ss]ans*","*[Hh]yundai*[Hh]armony*","*현대하모니*"]:
            hits = _glob.glob(os.path.join(d,"**",p),recursive=True) or _glob.glob(os.path.join(d,p))
            if hits:
                n = os.path.splitext(os.path.basename(hits[0]))[0]
                if "Head" in n: return "HyundaiSansHead"
                if "Text" in n: return "HyundaiSansText"
                return "현대하모니"
    return "맑은 고딕"


def _heat_index(Ta: float, RH: float) -> float:
    Tw = (Ta * math.atan(0.151977*(RH+8.313659)**0.5)
          + math.atan(Ta+RH) - math.atan(RH-1.67633)
          + 0.00391838*RH**1.5*math.atan(0.023101*RH) - 4.686035)
    return round(-0.2442+0.55399*Tw+0.45535*Ta-0.0022*Tw**2+0.00278*Tw*Ta+3.0, 1)


def week_label_ko(n: int) -> str:
    return ["첫째","둘째","셋째","넷째","다섯째"][min(n-1,4)] + "주"


def get_week_n(monday: date) -> int:
    """해당 월에서 몇 번째 주인지"""
    import calendar
    first = date(monday.year, monday.month, 1)
    mon   = first - timedelta(days=first.weekday())
    n = 1
    while mon < monday:
        mon += timedelta(days=7)
        n   += 1
    return n


def build_excel(records: list, meta: dict, monday: date) -> bytes:
    """records: DB에서 가져온 dict 리스트. 각 record에 _bytes(사진) 포함 가능."""
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "체감온도 기록관리 대장"
    FM = _excel_font()

    C_INK="1D1D1F"; C_BLUE="0071E3"; C_DARK="1D1D1F"; C_WHITE="FFFFFF"
    C_GRAY6="F5F5F7"; C_ROW0="FFFFFF"; C_ROW1="F9F9FB"; C_DATE="EBF3FF"; C_BORDER="C7C7CC"

    thin = Side(style="thin",color=C_BORDER)
    bdr  = Border(left=thin,right=thin,top=thin,bottom=thin)
    ctr  = Alignment(horizontal="center",vertical="center",wrap_text=False)
    lft  = Alignment(horizontal="left",  vertical="center",wrap_text=False,shrink_to_fit=True)
    SZ=10

    f_title = Font(bold=True,color=C_WHITE,name=FM,size=13)
    f_head  = Font(bold=True,color=C_WHITE,name=FM,size=SZ)
    f_meta  = Font(bold=True,color=C_INK,  name=FM,size=SZ)
    f_date  = Font(bold=True,color=C_BLUE, name=FM,size=SZ)
    f_body  = Font(         color=C_INK,  name=FM,size=SZ)
    f_sig   = Font(         color=C_INK,  name=FM,size=SZ)

    fill_title = PatternFill("solid",fgColor=C_DARK)
    fill_head  = PatternFill("solid",fgColor=C_BLUE)
    fill_photo = PatternFill("solid",fgColor="2C2C2E")
    fill_meta  = PatternFill("solid",fgColor=C_GRAY6)
    fill_sig   = PatternFill("solid",fgColor=C_GRAY6)
    fill_date  = PatternFill("solid",fgColor=C_DATE)
    fill_date1 = PatternFill("solid",fgColor="E3EDFF")
    lv_colors  = {"위험":("FF3B30",C_WHITE),"경고":("FF9500",C_WHITE),
                  "주의":("FFCC00",C_INK), "관심":("34C759",C_WHITE)}

    ROW_H=34; NCOL=11; PC=NCOL+1; PCW=22
    HDR=["작성일","구분","측정시각","온도(°C)","습도(%)","체감온도(°C)","단계","조치사항","기타내용","측정자","비고"]
    CWIDTHS=[10,7,9,7.5,7.5,10,8,17,13,10,13]
    for i,w in enumerate(CWIDTHS,1): ws.column_dimensions[get_column_letter(i)].width=w
    for i in range(4): ws.column_dimensions[get_column_letter(PC+i)].width=PCW

    wk_n = get_week_n(monday)
    wk_lbl = f"{monday.year}년 {monday.month}월 {week_label_ko(wk_n)}"

    # 행1: 제목
    ws.merge_cells(f"A1:{get_column_letter(PC+3)}1")
    c=ws["A1"]; c.value="체감온도 기록관리 대장"
    c.font=f_title; c.alignment=ctr; c.fill=fill_title; c.border=bdr
    ws.row_dimensions[1].height=38

    # 행2: 메타
    ws.merge_cells("A2:C2"); ws.merge_cells("D2:F2")
    ws.merge_cells("G2:I2"); ws.merge_cells("J2:K2")
    for addr,val in [("A2",f"현장명: {meta.get('현장명','')}"),
                     ("D2",f"업체명: {meta.get('업체명','')}"),
                     ("G2",f"측정위치: {meta.get('위치','')}"),
                     ("J2",wk_lbl)]:
        c=ws[addr]; c.value=val; c.font=f_meta; c.alignment=lft; c.border=bdr; c.fill=fill_meta
    ws.merge_cells(f"{get_column_letter(PC)}2:{get_column_letter(PC+3)}2")
    ph=ws[f"{get_column_letter(PC)}2"]
    ph.value="사진대지"; ph.fill=fill_photo; ph.font=f_head; ph.alignment=ctr; ph.border=bdr
    ws.row_dimensions[2].height=22

    # 행3: 헤더
    for ci,h in enumerate(HDR,1):
        c=ws.cell(row=3,column=ci,value=h)
        c.fill=fill_head; c.font=f_head; c.alignment=ctr; c.border=bdr
    for i,lbl in enumerate(SLOTS):
        c=ws.cell(row=3,column=PC+i,value=lbl)
        c.fill=fill_photo; c.font=f_head; c.alignment=ctr; c.border=bdr
    ws.row_dimensions[3].height=22

    dv=DataValidation(type="list",
        formula1='"N/A,추가휴식시간부여,보냉장구지급,작업시간대조정,작업중지,기타"',
        allow_blank=True,showDropDown=False)
    ws.add_data_validation(dv)

    # record map: (date_str, slot) → record
    rec_map: dict[tuple,dict] = {}
    for r in records:
        key = (r.get("measure_date","") or r.get("_date",""),
               r.get("slot","")         or r.get("_slot",""))
        rec_map[key] = r

    row_num=4
    for day_i in range(7):
        d=monday+timedelta(days=day_i); rs=row_num; re_=row_num+3
        even=(day_i%2==0)
        dfill=PatternFill("solid",fgColor=C_ROW0 if even else C_ROW1)

        for slot in SLOTS:
            rec=rec_map.get((d.isoformat(),slot),{})
            act=rec.get("action","") or rec.get("조치사항","")
            T  =rec.get("temperature") or rec.get("온도(°C)")
            RH =rec.get("humidity")    or rec.get("습도(%)")

            row_data=[f"{d.month}/{d.day}({DAYS_KO[d.weekday()]})",slot,
                      rec.get("measure_time","") or rec.get("측정시각",""),
                      T if T is not None else "", RH if RH is not None else "",
                      "","", act if act else "N/A",
                      rec.get("other_content","") or rec.get("기타내용","") if act=="기타" else "",
                      rec.get("measurer","") or rec.get("측정자",""),
                      rec.get("notes","")    or rec.get("비고","")]

            for ci,val in enumerate(row_data,1):
                c=ws.cell(row=row_num,column=ci,value=val)
                c.font=f_body; c.alignment=ctr; c.border=bdr; c.fill=dfill
                if ci in (4,5) and val!="": c.number_format="0.0"

            dr=f"D{row_num}"; er=f"E{row_num}"
            tw=(f"({dr}*ATAN(0.151977*SQRT({er}+8.313659))+ATAN({dr}+{er})"
                f"-ATAN({er}-1.67633)+0.00391838*{er}^1.5*ATAN(0.023101*{er})-4.686035)")
            fc=ws.cell(row=row_num,column=6)
            fc.value=(f'=IF(AND({dr}<>"",{er}<>""),ROUND(-0.2442+0.55399*{tw}'
                      f'+0.45535*{dr}-0.0022*{tw}^2+0.00278*{tw}*{dr}+3.0,1),"")')
            fc.font=f_body; fc.alignment=ctr; fc.border=bdr; fc.fill=dfill; fc.number_format="0.0"

            fr=f"F{row_num}"
            gc=ws.cell(row=row_num,column=7)
            gc.value=(f'=IF({fr}="","",IF({fr}>=38,"위험",IF({fr}>=35,"경고",'
                      f'IF({fr}>=33,"주의",IF({fr}>=31,"관심","-")))))')
            gc.font=f_body; gc.alignment=ctr; gc.border=bdr; gc.fill=dfill
            dv.add(ws.cell(row=row_num,column=8))
            row_num+=1

        ws.merge_cells(start_row=rs,start_column=1,end_row=re_,end_column=1)
        dc=ws.cell(row=rs,column=1)
        dc.font=f_date; dc.alignment=ctr
        dc.fill=fill_date if even else fill_date1; dc.border=bdr

        for si,slot in enumerate(SLOTS):
            col=PC+si
            ws.merge_cells(start_row=rs,start_column=col,end_row=re_,end_column=col)
            tc=ws.cell(row=rs,column=col); tc.border=bdr; tc.alignment=ctr; tc.fill=dfill
            rec=rec_map.get((d.isoformat(),slot))
            if rec and rec.get("_bytes"):
                try:
                    pil=Image.open(io.BytesIO(rec["_bytes"])).convert("RGB")
                    buf=io.BytesIO(); pil.save(buf,format="PNG"); buf.seek(0)
                    xi=XLImage(buf); anc=TwoCellAnchor()
                    anc._from=AnchorMarker(col=col-1,colOff=0,row=rs-1,rowOff=0)
                    anc.to   =AnchorMarker(col=col,  colOff=0,row=re_,  rowOff=0)
                    anc.editAs="twoCell"; xi.anchor=anc; ws.add_image(xi)
                except Exception: tc.value="⚠"

    for r in range(4,row_num): ws.row_dimensions[r].height=ROW_H

    g_range=f"G4:G{row_num-1}"
    for lv,(bg,fg) in lv_colors.items():
        ws.conditional_formatting.add(g_range,CellIsRule(
            operator="equal",formula=[f'"{lv}"'],
            fill=PatternFill("solid",fgColor=bg),
            font=Font(color=fg,bold=True,name=FM,size=SZ)))

    leg=row_num
    for lbl,c1,c2,bg,fg in [
        ("KOSHA 폭염 단계",1,2,C_DARK,C_WHITE),("■ 관심  31°C~",3,4,"34C759",C_WHITE),
        ("■ 주의  33°C~",5,6,"FFCC00",C_INK),  ("■ 경고  35°C~",7,8,"FF9500",C_WHITE),
        ("■ 위험  38°C~",9,11,"FF3B30",C_WHITE)]:
        ws.merge_cells(start_row=leg,start_column=c1,end_row=leg,end_column=c2)
        c=ws.cell(row=leg,column=c1,value=lbl)
        c.fill=PatternFill("solid",fgColor=bg)
        c.font=Font(color=fg,bold=True,name=FM,size=SZ); c.alignment=ctr; c.border=bdr
    ws.row_dimensions[leg].height=16; row_num+=1

    sig=row_num
    for label,c1,c2 in [("작성자",1,3),("검토자",4,7),("승인자",8,11)]:
        ws.merge_cells(start_row=sig,start_column=c1,end_row=sig,end_column=c2)
        c=ws.cell(row=sig,column=c1,value=f"{label}:                              (인)")
        c.border=bdr; c.alignment=Alignment(horizontal="left",vertical="top")
        c.font=f_sig; c.fill=fill_sig
    ws.row_dimensions[sig].height=ROW_H*1.6; row_num+=1

    ws.print_area=f"A1:{get_column_letter(PC+3)}{row_num-1}"
    ws.page_setup.orientation="landscape"; ws.page_setup.paperSize=9
    ws.page_setup.fitToPage=True; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=0
    ws.page_margins=PageMargins(left=0.4,right=0.4,top=0.5,bottom=0.5,header=0.2,footer=0.2)
    ws.print_title_rows="1:3"; ws.freeze_panes="A4"
    out=io.BytesIO(); wb.save(out); return out.getvalue()
