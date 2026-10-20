# -*- coding: utf-8 -*-
"""读世界，不建世界（world_state，2026-10-12 新增）。

设计纪律（承接用户本体论纠正）：
  - 系统不定义世界，只**读**世界。当前状态（CPI/PPI/10Y/LPR…）是世界给的具体观测值，
    不是系统构造的维度节点。
  - 本模块产出「当前世界状态」这一等对象：一组具体值 + 显式 raw/interpreted 标注 + provenance。
  - raw    = 世界直接给的数字（CPI 同比、PPI 同比、10Y 收益率、LPR、M2、汇率）。
  - interpreted = 从世界读数**推导**的状态（如 政策=偏松），必须带 provenance，
                 且明确标注「非原始数、是读出来的解释」，绝不伪装成 raw。
  - unknown = 世界尚未给够信息 / 源不可达 —— 诚实标 unknown，不填脑内规则。
  - 任何值失败都标 unknown + 原因，不编造。

输入：(signal, as_of)  —— 注意 signal 只用于选该读哪些世界读数，as_of 是观察时点。
输出：{"as_of", "signal", "state": {指标: {value, unit, as_of_data, source, kind, note}},
       "honesty": {...}}
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
sys.path.insert(0, TOOLS)

try:
    import akshare as ak
except Exception:
    ak = None


class _LazyPandas:
    """惰性 pandas 代理（2026-10-20 开源化落地）。

    为什么要懒加载：pandas 只用于「真实行情读取」这条支路（DataFrame 日期解析、
    最新行取值、数值缺失判定），纯逻辑推演链（世界状态演化 / 硬剪枝 / 树枚举）
    一次都不碰它。原先在模块顶层 `import pandas as pd`，会导致一个零依赖的
    逻辑引擎在别人 fresh clone 的机器上直接 ImportError 起不来——引擎明明
    不需要 pandas，却因为顶层 import 而要求所有人先装 pandas。

    这里改成：模块导入时不加载 pandas，只有真的走到行情支路并访问 pd 的属性
    时才 import；那时若环境确实没有 pandas，抛出可操作的安装提示，而不是
    在 import 阶段就炸掉整个引擎。
    """

    __slots__ = ()

    def __getattr__(self, name):
        try:
            import pandas as _pd
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise ImportError(
                "该功能需要 pandas（仅用于真实行情数据读取）。\n"
                "纯逻辑推演（world_tree / hard_prune / demo.py）不需要任何第三方库。\n"
                "如需真实行情支路，请先安装：pip install pandas\n"
                "原始错误：%s" % exc
            ) from exc
        return getattr(_pd, name)


pd = _LazyPandas()


def _unknown(kind="unknown", note="", source=""):
    return {"value": None, "unit": "", "as_of_data": "", "source": source,
            "kind": kind, "note": note}


def _date_col(df):
    """猜日期列名。"""
    for c in df.columns:
        cl = str(c).lower()
        if any(k in cl for k in ["日期", "date", "月份", "trade_date", "时间"]):
            return c
    return None


def _latest_row(df):
    """取「最新一行」：按日期列取最大日期那行（不同接口排序相反，不能无脑 iloc[0]）。"""
    if df is None or len(df) == 0:
        return None
    dc = _date_col(df)
    if dc:
        try:
            s = pd.to_datetime(df[dc], errors="coerce")
            if s.notna().any():
                return df.loc[s.idxmax()]
        except Exception:
            pass
    return df.iloc[0]  # 兜底


def _parse_month(s):
    """把 '2026年09月份' / '2026-09' 等解析为 Timestamp（取月初），解析失败返回 NaT。"""
    if s is None:
        return pd.NaT
    m = re.search(r"(\d{4})年(\d{1,2})", str(s))
    if m:
        try:
            return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
        except Exception:
            return pd.NaT
    m2 = re.search(r"(\d{4})[-/](\d{1,2})", str(s))
    if m2:
        try:
            return pd.Timestamp(int(m2.group(1)), int(m2.group(2)), 1)
        except Exception:
            return pd.NaT
    return pd.NaT


def _read_cpi():
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.macro_china_cpi()
        r = _latest_row(df)
        if r is None:
            return _unknown(note="CPI 接口返回空")
        # 列名含「全国-同比增长」（最新一期全国 CPI 同比）
        col = next((c for c in df.columns if "同比增长" in c and "全国" in c), None)
        if col is None:
            return _unknown(note="CPI 无「全国-同比增长」列；列=%s" % list(df.columns))
        return {
            "value": float(r[col]), "unit": "%",
            "as_of_data": str(r.get("月份", "")), "source": "国家统计局(CPI)",
            "kind": "raw", "note": "全国 CPI 同比，世界给的真实读数",
        }
    except Exception as e:
        return _unknown(note="CPI 取数失败: %s" % e)


def _read_ppi():
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.macro_china_ppi()
        r = _latest_row(df)
        if r is None:
            return _unknown(note="PPI 接口返回空")
        col = next((c for c in df.columns if "同比增长" in c and ("当月" in c or "全国" in c)), None)
        if col is None:
            col = next((c for c in df.columns if "同比增长" in c), None)
        if col is None:
            return _unknown(note="PPI 无同比增长列；列=%s" % list(df.columns))
        return {
            "value": float(r[col]), "unit": "%",
            "as_of_data": str(r.get("月份", "")), "source": "国家统计局(PPI)",
            "kind": "raw", "note": "PPI 当月同比，世界给的真实读数",
        }
    except Exception as e:
        return _unknown(note="PPI 取数失败: %s" % e)


def _read_lpr():
    if ak is None:
        return _unknown(note="akshare 不可用"), _unknown(note="akshare 不可用")
    try:
        df = ak.macro_china_lpr()
        r = _latest_row(df)
        if r is None:
            return _unknown(note="LPR 接口返回空"), _unknown(note="LPR 接口返回空")
        c1 = "LPR1Y" if "LPR1Y" in df.columns else next((c for c in df.columns if "1Y" in c.upper()), None)
        c5 = "LPR5Y" if "LPR5Y" in df.columns else next((c for c in df.columns if "5Y" in c.upper()), None)
        asof = str(r.get("TRADE_DATE", r.get("日期", "")))
        v1 = float(r[c1]) if (c1 and pd.notna(r[c1])) else None
        v5 = float(r[c5]) if (c5 and pd.notna(r[c5])) else None
        o1 = {"value": v1, "unit": "%", "as_of_data": asof, "source": "央行(LPR)",
              "kind": "raw", "note": "1年期 LPR，世界给的真实读数"} if v1 is not None else _unknown(note="LPR 无 1Y 值")
        o5 = {"value": v5, "unit": "%", "as_of_data": asof, "source": "央行(LPR)",
              "kind": "raw", "note": "5年期 LPR，世界给的真实读数"} if v5 is not None else _unknown(note="LPR 无 5Y 值")
        return o1, o5
    except Exception as e:
        return _unknown(note="LPR 取数失败: %s" % e), _unknown(note="LPR 取数失败: %s" % e)


def _read_m2():
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.macro_china_m2_yearly()
        if df is None or len(df) == 0:
            return _unknown(note="M2 接口返回空")
        col = "今值" if "今值" in df.columns else next((c for c in df.columns if "同比" in c or "M2" in c), None)
        if col is None:
            return _unknown(note="M2 无数值列；列=%s" % list(df.columns))
        dc = _date_col(df)
        # 先取最新一期；若最新一期尚未公布(空)，回退到最近一期非空（世界给的真实读数）
        r = _latest_row(df)
        if dc and pd.isna(r.get(col)):
            desc = df.assign(_d=pd.to_datetime(df[dc], errors="coerce")).sort_values("_d", ascending=False)
            for _, rr in desc.iterrows():
                if pd.notna(rr[col]):
                    r = rr
                    break
        if pd.isna(r.get(col)):
            return _unknown(note="M2 全部 今值 为空")
        return {
            "value": float(r[col]), "unit": "%",
            "as_of_data": str(r.get("日期", "")), "source": "央行(M2)",
            "kind": "raw", "note": "M2 同比，世界给的真实读数",
        }
    except Exception as e:
        return _unknown(note="M2 取数失败: %s" % e)


def _read_exchange():
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        # 离岸人民币即期：中国银行即期牌价（akshare 无 macro_china_usd_cnh）
        df = ak.currency_boc_sina()
        sub = df[df.astype(str).apply(lambda c: c.str.contains("美元", na=False)).any(axis=1)]
        if sub is None or len(sub) == 0:
            return _unknown(note="currency_boc_sina 无美元行")
        r = sub.iloc[0]
        price_col = next((c for c in df.columns if any(k in c for k in
                       ["现汇卖出", "中行折算", "现汇买入", "卖出价", "买入价"])), None)
        if price_col is None:
            price_col = df.columns[-1]
        v = float(r[price_col]) if pd.notna(r[price_col]) else None
        if v is None:
            return _unknown(note="美元价格列空")
        return {
            "value": v, "unit": "CNY/USD",
            "as_of_data": str(r.get("日期", r.get("date", ""))), "source": "中国银行即期牌价",
            "kind": "raw", "note": "美元兑人民币(中行即期)，世界给的真实读数",
        }
    except Exception as e:
        return _unknown(note="汇率取数失败: %s" % e)


def _read_cn10y():
    """中国 10 年期国债收益率（中债收益率曲线）。

    诚实采集纪律（承接用户反复纠正）：真源 bond_china_yield 走 chinabond，本环境至今 TLS 失败。
    cn10y_feed 负责「真源优先 + 缓存兜底 + 不可达时 unknown」，绝不把脑内规则伪装成实时读数。
    """
    try:
        from cn10y_feed import fetch_cn10y
        return fetch_cn10y()
    except Exception as e:
        return _unknown(kind="unknown", note="CN10Y 采集器异常: %s" % e)


def _read_enso():
    """ENSO（厄尔尼诺-南方涛动）相位：NOAA CPC 官方 ONI（Niño-3.4 海温距平）。

    诚实采集纪律（同 cn10y）：真源 NOAA ONI 经 requests 取（urllib 证书坑绕开），
    enso_feed 负责「真源优先 + 缓存兜底 + 不可达时 unknown」，绝不把脑内规则伪装成实时读数。
    注意：本函数只负责把「世界真实给的 ENSO 读数」接进世界状态；ENSO 与宏观指标之间的
    传导（气候→农产品/能源/通胀）属 factor graph 边（#234 ② 未做），本函数不派生因果。
    """
    try:
        from enso_feed import fetch_enso
        return fetch_enso()
    except Exception as e:
        return _unknown(kind="unknown", note="ENSO 采集器异常: %s" % e)


def _latest_valid(df, date_col, value_col):
    """取「最新一期且数值非空」的那行（不同接口排序相反，按日期列取最大日期）。"""
    if df is None or len(df) == 0 or value_col not in df.columns:
        return None
    dc = date_col if date_col in df.columns else _date_col(df)
    if dc:
        s = pd.to_datetime(df[dc], errors="coerce")
        df2 = df.assign(_d=s)
    else:
        df2 = df.assign(_d=pd.Series(range(len(df)), index=df.index))
    valid = df2[df2[value_col].notna()].sort_values("_d", ascending=False)
    return valid.iloc[0] if len(valid) else None


def _read_us(ak_fn, date_col, value_col, label, source):
    """通用美国宏观读数读取器：取最新有效值，命名空间=US，失败 honest unknown。"""
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak_fn()
        r = _latest_valid(df, date_col, value_col)
        if r is None:
            return _unknown(
                note="%s 接口无有效读数（列=%s）" % (label, list(df.columns) if df is not None else "None"))
        v = float(r[value_col])
        asof = str(r.get(date_col, ""))
        return {
            "value": v, "unit": "%",
            "as_of_data": asof, "source": source,
            "kind": "raw",
            "note": "%s，世界给的真实读数（命名空间=US）" % label,
        }
    except Exception as e:
        return _unknown(note="%s 取数失败: %s" % (label, e))


def _read_us_cpi_yoy():
    return _read_us(ak.macro_usa_cpi_yoy, "时间", "现值",
                    "美国CPI同比", "BLS(akshare macro_usa_cpi_yoy)")


def _read_us_cpi_mom():
    return _read_us(ak.macro_usa_cpi_monthly, "日期", "今值",
                    "美国CPI环比", "BLS(akshare macro_usa_cpi_monthly)")


def _read_us_core_cpi_mom():
    return _read_us(ak.macro_usa_core_cpi_monthly, "日期", "今值",
                    "美国核心CPI环比", "BLS(akshare macro_usa_core_cpi_monthly)")


def _read_us_ppi():
    return _read_us(ak.macro_usa_ppi, "日期", "今值",
                    "美国PPI", "BLS(akshare macro_usa_ppi)")


def _read_us_fedfunds():
    return _read_us(ak.macro_bank_usa_interest_rate, "日期", "今值",
                    "美联储利率", "Fed(akshare macro_bank_usa_interest_rate)")


def _read_us10y():
    return _read_us(ak.bond_zh_us_rate, "日期", "美国国债收益率10年",
                    "美国10Y国债收益率", "Treasury(akshare bond_zh_us_rate)")


# ── A股资金面（内生流动性；本案盲区修复②：让系统读到 A股自身的钱）──
def _read_cn_turnover():
    """沪深A股总成交额（亿元）。东财 stock_zh_a_spot_em 为准，失败回退新浪 stock_zh_a_spot。
    命名空间=CN；为真实观测值(raw)。"""
    if ak is None:
        return _unknown(note="akshare 不可用")
    src, val = None, None
    try:
        df = ak.stock_zh_a_spot_em()
        if df is not None and len(df) and "成交额" in df.columns:
            val = df["成交额"].sum() / 1e8
            src = "东方财富(stock_zh_a_spot_em)"
    except Exception:
        pass
    if val is None:
        try:
            df = ak.stock_zh_a_spot()
            if df is not None and len(df) and "成交额" in df.columns:
                val = df["成交额"].sum() / 1e8
                src = "新浪(stock_zh_a_spot)"
        except Exception:
            pass
    if val is not None:
        return {"value": round(val, 1), "unit": "亿元", "as_of_data": "当日",
                "source": src, "kind": "raw",
                "note": "沪深A股总成交额（当日，元→亿元），世界给的真实观测"}
    return _unknown(note="成交额源不可达（东方财富/新浪均失败或限流）")


def _read_cn_margin():
    """沪深两融(融资融券)余额合计（亿元）：上交所 + 深交所各自最新一期求和。
    命名空间=CN；为真实观测值(raw)。"""
    if ak is None:
        return _unknown(note="akshare 不可用")
    total, asof, parts = 0.0, [], []
    for fn, exch in ((ak.macro_china_market_margin_sh, "上交所"),
                    (ak.macro_china_market_margin_sz, "深交所")):
        try:
            df = fn()
            if df is None or len(df) == 0 or "融资融券余额" not in df.columns:
                continue
            r = df.sort_values("日期").iloc[-1]
            v = float(r["融资融券余额"]) / 1e8
            total += v
            asof.append(str(r["日期"])[:10])
            parts.append("%s=%.1f" % (exch, v))
        except Exception:
            continue
    if total > 0:
        return {"value": round(total, 1), "unit": "亿元", "as_of_data": "/".join(sorted(set(asof))),
                "source": "上交所+深交所两融(macro_china_market_margin_sh/sz)", "kind": "raw",
                "note": "沪深两融余额合计（%s）" % " + ".join(parts)}
    return _unknown(note="两融源不可达（上交所/深交所）")


def _read_cn_m1():
    """M1 同比（%）：货币供应量的活化程度，反映企业/交易性资金活跃度。
    源 macro_china_money_supply（月份为 'YYYY年MM月份' 字符串，需解析）；M2 同比一并附在 note。"""
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.macro_china_money_supply()
        if df is None or len(df) == 0:
            return _unknown(note="M1 接口返回空")
        d = df.copy()
        d["_m"] = d["月份"].map(lambda s: _parse_month(s))
        d = d.dropna(subset=["_m"]).sort_values("_m")
        if len(d) == 0:
            return _unknown(note="M1 月份解析失败")
        r = d.iloc[-1]
        col = "货币(M1)-同比增长"
        if col not in r:
            return _unknown(note="M1 无同比增长列；列=%s" % list(df.columns))
        m2 = r.get("货币和准货币(M2)-同比增长")
        try:
            m2s = "%.1f" % float(m2) if pd.notna(m2) else "—"
        except Exception:
            m2s = "—"
        return {"value": float(r[col]), "unit": "%",
                "as_of_data": str(r.get("月份", "")),
                "source": "央行(货币供应量表)", "kind": "raw",
                "note": "M1 同比（活化货币）；M2 同比=%s%%" % m2s}
    except Exception as e:
        return _unknown(note="M1 取数失败: %s" % e)


# ── 跨市场/全球（外生冲击观测；本案盲区修复③：让系统读到全球风险资产共振）──
def _read_index_daily(code, label, source):
    """新浪美股指数日线（纳指 .IXIC / 标普 .INX / 道指 .DJI）：取最新收盘 + 自算日涨跌幅。
    命名空间=GLOBAL；为真实观测值(raw)。"""
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.stock_us_daily(symbol=code)
        if df is None or len(df) < 2 or "close" not in df.columns:
            return _unknown(note="%s 接口返回不足两行或无色close列" % label)
        df = df.sort_values("date") if "date" in df.columns else df
        c0, c1 = float(df["close"].iloc[-2]), float(df["close"].iloc[-1])
        chg = (c1 - c0) / c0 * 100 if c0 else None
        asof = str(df["date"].iloc[-1])
        return {"value": round(c1, 2), "unit": "点",
                "as_of_data": asof, "source": source, "kind": "raw",
                "note": "%s最新收盘；当日涨跌幅 %s%%（自算：前收→今收）" % (
                    label, ("%.2f" % chg) if chg is not None else "—")}
    except Exception as e:
        return _unknown(note="%s 取数失败: %s" % (label, e))


def _read_index_feed(ak_fn, label, source):
    """东方财富指数 feed（SOX/能源指数）：含『涨跌幅』列，取最新一行。"""
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak_fn()
        if df is None or len(df) == 0:
            return _unknown(note="%s 接口返回空" % label)
        r = df.iloc[-1]
        val = float(r["最新值"]) if "最新值" in r and pd.notna(r.get("最新值")) else None
        chg = float(r["涨跌幅"]) if "涨跌幅" in r and pd.notna(r.get("涨跌幅")) else None
        asof = str(r.get("日期", ""))
        return {"value": round(val, 2) if val is not None else None, "unit": "点",
                "as_of_data": asof, "source": source, "kind": "raw",
                "note": "%s最新值；当日涨跌幅 %s%%" % (
                    label, ("%.2f" % chg) if chg is not None else "—")}
    except Exception as e:
        return _unknown(note="%s 取数失败: %s" % (label, e))


def _read_sox():
    return _read_index_feed(ak.macro_global_sox_index, "费城半导体指数(SOX)",
                             "ICE(akshare macro_global_sox_index)")


def _read_energy_index():
    # 诚实标注：能源指数 ≠ 布伦特现货；akshare 无布伦特干净源，本接口作「能源价格方向」真实代理
    return _read_index_feed(ak.macro_china_energy_index, "能源指数",
                            "东方财富(macro_china_energy_index)")


def _read_us_report_rev(stock, label):
    """美股单票最新单季财报主营收入（东方财富美股财报）。命名空间=GLOBAL；真实观测(raw)。"""
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.stock_financial_us_report_em(stock=stock, symbol="综合损益表", indicator="单季报")
        if df is None or len(df) == 0:
            return _unknown(note="%s 财报返回空" % label)
        sub = df[df["ITEM_NAME"].astype(str).str.contains("主营收入", na=False)]
        if len(sub) == 0:
            return _unknown(note="%s 财报无『主营收入』项" % label)
        sub = sub.copy()
        sub["_d"] = pd.to_datetime(sub["REPORT_DATE"], errors="coerce")
        r = sub.sort_values("_d", ascending=False).iloc[0]
        amt = float(r["AMOUNT"])
        return {"value": amt, "unit": "原始金额", "as_of_data": str(r.get("REPORT_DATE", "")),
                "source": "东方财富美股财报(%s)" % stock, "kind": "raw",
                "note": "%s 最新单季(REPORT=%s)主营收入；币种以公司财报为准（ASML=欧元 / TSM=新台币）" % (
                    label, r.get("REPORT"))}
    except Exception as e:
        return _unknown(note="%s 财报取数失败: %s" % (label, e))


# ── A股行业/概念板块指数（市场真实节点；本案盲区修复④：让系统读到电力/AI板块真实联动）──
def _read_cn_board_idx(code, label, source):
    """A股行业/概念板块指数日线（东财 stock_board_industry_hist_em，按板块代码拉，行业/概念通用）。
    取最新收盘 + 自算日涨跌幅；命名空间=CN；为真实观测值(raw)。
    诚实标注：板块指数代表行业/概念整体方向，个股分化不体现；电价无干净真源，
    故本节点是「电力/AI板块的市场暴露」真实读数，非「电价」本身。
    """
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.stock_board_industry_hist_em(
            symbol=code, period="日k", start_date="20230101", end_date="20261231")
        if df is None or len(df) < 2 or "收盘" not in df.columns:
            return _unknown(note="%s 板块日线返回不足或无色收盘列" % label)
        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        df = df.dropna(subset=["日期"]).sort_values("日期")
        c0, c1 = float(df["收盘"].iloc[-2]), float(df["收盘"].iloc[-1])
        chg = (c1 - c0) / c0 * 100 if c0 else None
        asof = str(df["日期"].iloc[-1].date())
        return {"value": round(c1, 2), "unit": "点", "as_of_data": asof,
                "source": source, "kind": "raw",
                "note": "%s板块指数最新收盘；当日涨跌幅 %s%%（自算：前收→今收）。板块指数代表行业整体方向，个股分化不体现" %
                        (label, ("%.2f" % chg) if chg is not None else "—")}
    except Exception as e:
        return _unknown(note="%s 板块指数取数失败: %s" % (label, e))


def _read_cn_agri_price_idx():
    """农产品批发价格（菜篮子）指数日频真源（akshare macro_china_agricultural_product）。
    这是「普通人菜价」的上游真实读数：批发价指数→零售菜价→普通人菜篮子。
    诚实标注：① 为批发价指数(农业部发布)，非零售食品CPI分项（akshare 无食品CPI分项真源）；
    ② 指数级市场↔市场传导不覆盖此节点，气候→菜价属传导图 none/hypothesized 层。
    列：日期/最新值/涨跌幅(日)/近3月/6月/1年/2年/3年涨跌幅。
    """
    if ak is None:
        return _unknown(note="akshare 不可用")
    try:
        df = ak.macro_china_agricultural_product()
        if df is None or len(df) < 2 or "最新值" not in df.columns:
            return _unknown(note="农产品批发价指数返回不足或无色最新值列")
        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        df = df.dropna(subset=["日期"]).sort_values("日期")
        lvl = float(df["最新值"].iloc[-1])
        dchg = df["涨跌幅"].iloc[-1]
        yoy = df["近1年涨跌幅"].iloc[-1] if "近1年涨跌幅" in df.columns else None
        asof = str(df["日期"].iloc[-1].date())
        return {"value": round(lvl, 2), "unit": "点", "as_of_data": asof,
                "source": "akshare macro_china_agricultural_product(农产品批发价格/菜篮子指数,日频)",
                "kind": "raw",
                "note": "农产品批发价指数最新值；当日涨跌幅 %s%%、近1年 %s%%。为批发价指数(农业部发布)，非零售食品CPI分项；是『普通人菜价』上游真实读数" %
                        (("%.2f" % dchg) if pd.notna(dchg) else "—",
                         ("%.2f" % yoy) if pd.notna(yoy) else "—")}
    except Exception as e:
        return _unknown(note="农产品批发价指数取数失败: %s" % e)


# 信号判读：命中海外/美国关键词 → 锚点走 US 命名空间（避免误用中国 CPI）
_US_ANCHOR_KW = re.compile(
    r"美联储|美国|美股|全球|海外|纳指|费半|美债|美元|UST|ASML|TSMC|台积电|阿斯麦|标普|道指|非农|加息")


def _route_anchor(signal, state):
    """按信号路由宏观锚点：海外驱动→美国CPI，否则中国CPI。返回顶部 anchor_cpi 字段。"""
    us_scope = bool(_US_ANCHOR_KW.search(signal or ""))
    src = state["US_CPI_YOY"] if us_scope else state["CPI"]
    return {
        "scope": "US" if us_scope else "CN",
        "value": src.get("value"),
        "unit": src.get("unit"),
        "as_of_data": src.get("as_of_data"),
        "source": src.get("source"),
        "kind": src.get("kind"),
        "note": ("信号判读为海外/美国驱动 → 锚定美国CPI（避免误用中国CPI 0.9%）"
                 if us_scope else "信号非海外驱动 → 锚定中国CPI"),
    }


def _read_policy_stance(lpr_1y):
    """政策立场 = 从世界读数推导的解释性状态，绝非脑内规则。

    证据来自 LPR 水平与近期方向（世界给的）；标注 interpreted + provenance。
    权威立场需读央行货政报告/新闻发布会表述，本模块未接 PBC 文本源，故若 LPR 不可得则 unknown。
    """
    if lpr_1y.get("value") is None:
        return _unknown(
            kind="unknown",
            note="LPR 不可得，无法从世界读数推导政策立场；需接 PBC 文本源才能读立场",
            source="derive-from-LPR")
    lvl = lpr_1y["value"]
    # 解释性判读：仅基于 LPR 真实水平给出弱信号，明确是 interpreted 非 raw
    if lvl <= 3.1:
        stance = "偏松"
    elif lvl >= 3.65:
        stance = "偏紧"
    else:
        stance = "中性"
    return {
        "value": stance, "unit": "",
        "as_of_data": lpr_1y.get("as_of_data", ""),
        "source": "derive-from-LPR(%s%%)" % lvl,
        "kind": "interpreted",
        "note": ("由 1Y LPR=%.2f%% 推导的弱信号；非原始数、非脑内规则。"
                 "权威立场需读央行表述（PBC 文本源未接）" % lvl),
    }


def read_world_state(signal, as_of=None):
    """读回当前世界状态（具体观测值，非维度节点）。"""
    as_of = as_of or datetime.date.today().isoformat()
    lpr1, lpr5 = _read_lpr()
    state = {
        "CPI": _read_cpi(),
        "PPI": _read_ppi(),
        "LPR_1Y": lpr1,
        "LPR_5Y": lpr5,
        "M2": _read_m2(),
        "USDCNH": _read_exchange(),
        "CN10Y": _read_cn10y(),
        "ENSO": _read_enso(),
        "policy_stance": _read_policy_stance(lpr1),
        # ── 命名空间 US：美国宏观（本案盲区修复）──
        "US_CPI_YOY": _read_us_cpi_yoy(),
        "US_CPI_MOM": _read_us_cpi_mom(),
        "US_CORE_CPI_MOM": _read_us_core_cpi_mom(),
        "US_PPI": _read_us_ppi(),
        "US_FEDFUNDS": _read_us_fedfunds(),
        "US_10Y": _read_us10y(),
        # ── A股资金面（内生流动性；本案盲区修复②）──
        "CN_TURNOVER": _read_cn_turnover(),
        "CN_MARGIN_BALANCE": _read_cn_margin(),
        "CN_M1": _read_cn_m1(),
        # ── 跨市场/全球（外生冲击观测；本案盲区修复③）──
        "G_NASDAQ": _read_index_daily(".IXIC", "纳斯达克指数", "新浪美股(.IXIC)"),
        "G_SOX": _read_sox(),
        "G_ENERGY_IDX": _read_energy_index(),
        "G_KOSPI": _unknown(note="akshare 无韩国 KOSPI 实时接口；本案韩国仅作亚太共振旁证，诚实留空"),
        "G_ASML_REV": _read_us_report_rev("ASML", "阿斯麦(ASML)"),
        "G_TSM_REV": _read_us_report_rev("TSM", "台积电(TSM)"),
        # ── A股行业/概念板块指数（市场真实节点；本案盲区修复④）──
        # 诚实标注：是「板块市场暴露」真实读数，非「电价」本身（电价无干净真源）
        "CN_POWER_IDX": _read_cn_board_idx("BK0428", "电力", "东方财富行业板块(电力 BK0428)"),
        "CN_AI_IDX": _read_cn_board_idx("BK0800", "人工智能", "东方财富概念板块(人工智能 BK0800)"),
        # ── 普通人终端：农产品批发价（菜篮子）指数日频真源 ──
        # 诚实标注：批发价指数(农业部发布)，非零售食品CPI分项；是『普通人菜价』上游真实读数
        "CN_AGRI_PRICE_IDX": _read_cn_agri_price_idx(),
    }
    n_raw = sum(1 for v in state.values() if v.get("kind") == "raw" and v.get("value") is not None)
    n_interp = sum(1 for v in state.values() if v.get("kind") == "interpreted" and v.get("value") is not None)
    n_unknown = sum(1 for v in state.values() if v.get("kind") in ("unknown",) or v.get("value") is None)
    anchor_cpi = _route_anchor(signal, state)
    return {
        "as_of": as_of,
        "signal": signal,
        "state": state,
        "anchor_cpi": anchor_cpi,
        "namespace": {
            "CN": ["CPI", "PPI", "M2", "LPR_1Y", "LPR_5Y", "CN10Y", "USDCNH", "policy_stance", "ENSO",
                   "CN_TURNOVER", "CN_MARGIN_BALANCE", "CN_M1", "CN_POWER_IDX", "CN_AI_IDX",
                   "CN_AGRI_PRICE_IDX"],
            "US": ["US_CPI_YOY", "US_CPI_MOM", "US_CORE_CPI_MOM", "US_PPI", "US_FEDFUNDS", "US_10Y"],
            "GLOBAL": ["G_NASDAQ", "G_SOX", "G_ENERGY_IDX", "G_KOSPI", "G_ASML_REV", "G_TSM_REV"],
            "note": "CN=中国宏观（国家统计局/央行/中债）+ A股资金面（成交额/两融/M1）；"
                    "US=美国宏观（BLS/Fed/Treasury）；"
                    "GLOBAL=跨市场/全球（纳指/费半SOX/能源指数/ASML·TSM财报；KOSPI诚实留空）；"
                    "anchor_cpi 按信号路由到对应命名空间，避免'中国CPI'被误作'美国通胀冲击'锚点",
        },
        "honesty": {
            "principle": "读世界，不建世界；值为世界给的观测，非系统定义",
            "raw": n_raw, "interpreted": n_interp, "unknown": n_unknown,
            "anchor_scope": anchor_cpi["scope"],
            "note": "interpreted 项(如 policy_stance)是从世界读数推导的解释，带 provenance；"
                    "unknown 项=世界未给够信息或源不可达，不填脑内规则。"
                    "CN/US 命名空间分离：海外驱动信号锚 US_CPI_YOY，否则锚 CPI(中国)。",
        },
    }


def main():
    import argparse
    ap = argparse.ArgumentParser(description="读当前世界状态（具体观测值，非维度节点）")
    ap.add_argument("--signal", default="9月CPI PPI居民消费价格数据")
    ap.add_argument("--as-of", default=datetime.date.today().isoformat())
    ap.add_argument("--out", default=os.path.join(BASE, "reports", "world_state.json"))
    a = ap.parse_args()
    ws = read_world_state(a.signal, a.as_of)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(ws, f, ensure_ascii=False, indent=2)
    print("当前世界状态 @ %s （信号=%s）" % (ws["as_of"], ws["signal"]))
    for k, v in ws["state"].items():
        val = v.get("value")
        print("  %-12s %-8s %-6s %s" % (
            k, (str(val) if val is not None else "—"),
            v.get("kind"), v.get("note", "")))
    print("诚实口径:", ws["honesty"])
    print("->", os.path.relpath(a.out, BASE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
