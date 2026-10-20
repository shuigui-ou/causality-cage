# -*- coding: utf-8 -*-
"""存储层：SQLite（可变状态 + 唯一权威源）与 Parquet（只追加）。

存储分层裁决见 §12.1：**逻辑 5 类对象 = 物理 6 张表**（`[H20]` 追加 `baseline_series` 后）。
    SQLite : audit_events（唯一权威源）、chains、signals_state（物化投影）
    Parquet: signals_raw（first-write-wins）、rejection_cases（只追加）、baseline_series（只追加）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..paths import (BASELINE_SERIES_DIR, REJECTION_CASES_DIR, SIGNALS_RAW_DIR,
                     SQLITE_PATH)
from .append_only import append_first_write_wins, read_all

# ---- §12.1 逻辑对象的**单一真源** ----
# 为什么放在这里而不是写在报告里：`physical_tables` 是**派生**的（list(SQLITE_TABLES)），
# 而 `logical_objects` 曾经是**硬编码列表**——两者一个派生一个硬编码，就会漂移。
# 实测发生过（`[H27]`）：`[H20]` 追加第 5 类对象 `baseline_series` 时，
# 规格 §12.1 与 DDL 都同步了，**报告载荷的硬编码列表没同步**，
# 于是载荷声明的逻辑对象数比物理表所对应的对象数少一个——**自相矛盾**。
# （此处刻意**不逐字写出**旧文案：注释也在扫描面内，逐字复现会让「旧文案不得残留」
#   这条断言把注释本身当成命中——`[H24]` 的复发形态，实测已累计四次。）
# → 钉成常量，由 `tests/conformance.py` 组 [13] 断言它与 §12.1 的声明一致（回退即红）。
LOGICAL_OBJECTS = ("signals", "chains", "rejection_cases", "baseline_series", "audit_events")

# ---- DDL ----

DDL = {
    # §1.3 可审计最小字段。chain_id 为超集扩展（§1.3 是"最小"要求，允许有更多字段）
    "audit_events": """
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id            TEXT PRIMARY KEY,
            signal_id           TEXT,
            chain_id            TEXT,
            from_status         TEXT,
            to_status           TEXT NOT NULL,
            rule_applied        TEXT NOT NULL,
            rule_version        TEXT NOT NULL,
            model_version       TEXT NOT NULL,
            input_snapshot_hash TEXT,
            at                  TEXT NOT NULL
        )""",
    # §5.2 每级字段以 JSON 数组存放；§5.3 的知识图谱降级事实必须落盘，不得静默跳过
    "chains": """
        CREATE TABLE IF NOT EXISTS chains (
            chain_id              TEXT PRIMARY KEY,
            signal_id             TEXT NOT NULL,
            status                TEXT NOT NULL,
            current_level         INTEGER,
            levels_json           TEXT,
            confidence            TEXT,
            weakest_level         INTEGER,
            kg_available          INTEGER NOT NULL DEFAULT 0,
            unverified_assumption INTEGER NOT NULL DEFAULT 0,
            created_at            TEXT,
            updated_at            TEXT
        )""",
    # signals 的可变投影：status 在 §1.2 的 10 个枚举间迁移
    # 权威源仍是 audit_events；本表不一致时以 audit_events 为准并告警（§12.1）
    "signals_state": """
        CREATE TABLE IF NOT EXISTS signals_state (
            signal_id     TEXT PRIMARY KEY,
            source_type   TEXT,
            source_name   TEXT,
            status        TEXT NOT NULL,
            stage         INTEGER,
            outbreak_score REAL,
            entity        TEXT,
            action        TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_dt  TEXT,
            observations  INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT NOT NULL,
            assumed_params TEXT
        )""",
}

# 轻量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列
MIGRATIONS = (
    "ALTER TABLE signals_state ADD COLUMN last_seen_dt TEXT",
    "ALTER TABLE signals_state ADD COLUMN observations INTEGER NOT NULL DEFAULT 0",
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_audit_signal ON audit_events(signal_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_at     ON audit_events(at)",
    "CREATE INDEX IF NOT EXISTS ix_state_status ON signals_state(status)",
    "CREATE INDEX IF NOT EXISTS ix_state_source ON signals_state(source_type)",
    "CREATE INDEX IF NOT EXISTS ix_chain_signal ON chains(signal_id)",
)

# Parquet 侧的表清单
#   signals_raw      条目 × 采集期 的观察记录（幂等键 = observation_key）
#   rejection_cases  §7 负样本库
#   baseline_series  每源每期的聚合值 —— §14.6 W3 的 s_hat 与 §2.2 的阶段门禁都依赖它
PARQUET_TABLES = {
    "signals_raw": lambda: SIGNALS_RAW_DIR,
    "rejection_cases": lambda: REJECTION_CASES_DIR,
    "baseline_series": lambda: BASELINE_SERIES_DIR,
}

SQLITE_TABLES = tuple(DDL)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    p = path or SQLITE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path=None) -> dict:
    """建 6 张物理表（§12 第 1 天第 1 条 / §12.1）。幂等。"""
    created = []
    conn = connect(path)
    with conn:
        for name, ddl in DDL.items():
            conn.execute(ddl)
            created.append(name)
        for mig in MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass                                   # 列已存在
        for ix in INDEXES:
            conn.execute(ix)
    conn.close()
    for base_fn in PARQUET_TABLES.values():
        base_fn().mkdir(parents=True, exist_ok=True)
    return {
        "sqlite": list(created),
        "parquet_dirs": sorted(str(base_fn()) for base_fn in PARQUET_TABLES.values()),
    }


# ---- 写审计事件（§13.2：唯一权威源）----

def write_audit(conn, events) -> int:
    if not events:
        return 0
    rows = [
        (e["event_id"], e.get("signal_id"), e.get("chain_id"),
         e.get("from_status"), e["to_status"], e["rule_applied"],
         e["rule_version"], e.get("model_version", "deterministic"),
         e.get("input_snapshot_hash"), e.get("at") or now_iso())
        for e in events
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO audit_events"
            " (event_id, signal_id, chain_id, from_status, to_status,"
            "  rule_applied, rule_version, model_version, input_snapshot_hash, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


# ---- 写 signals_state ----

def upsert_signals_state(conn, rows) -> int:
    if not rows:
        return 0
    payload = [
        (r["signal_id"], r.get("source_type"), r.get("source_name"), r["status"],
         r.get("stage"), r.get("outbreak_score"), r.get("entity"), r.get("action"),
         r.get("first_seen_at") or now_iso(), r.get("last_seen_dt"), r.get("observations") or 0,
         r.get("updated_at") or now_iso(),
         json.dumps(r.get("assumed_params", []), ensure_ascii=False))
        for r in rows
    ]
    with conn:
        conn.executemany(
            "INSERT INTO signals_state (signal_id, source_type, source_name, status, stage,"
            " outbreak_score, entity, action, first_seen_at, last_seen_dt, observations,"
            " updated_at, assumed_params)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(signal_id) DO UPDATE SET"
            "  status=excluded.status, stage=excluded.stage,"
            "  outbreak_score=COALESCE(excluded.outbreak_score, signals_state.outbreak_score),"
            "  last_seen_dt=excluded.last_seen_dt, observations=excluded.observations,"
            "  updated_at=excluded.updated_at, assumed_params=excluded.assumed_params",
            payload)
    return len(payload)


def known_signal_ids(conn) -> set:
    """已存在于 `signals_state` 的 signal_id。

    用途：判定「本周期**首次出现**的信号」。跨期重复观察同一 native_id **不算新信号**
    （§2.3：超 48h 不合并 → 是新周期，但「新周期」不等于「新信号」；
     本实现的取舍是把重复观察计入 `observations`，不计入 `signals_new`，
     这样 §9.2 的 `signals_new = signals_scored + signals_baseline` 才成立）。
    """
    return {row["signal_id"] for row in conn.execute("SELECT signal_id FROM signals_state")}


def touch_signals(conn, pairs) -> int:
    """对已存在的信号累加观察次数、更新 last_seen_dt（不写审计——状态未变，§1.3）。"""
    if not pairs:
        return 0
    with conn:
        conn.executemany(
            "UPDATE signals_state SET observations = observations + ?, last_seen_dt = ?,"
            " updated_at = ? WHERE signal_id = ?",
            [(n, dt, now_iso(), sid) for sid, dt, n in pairs])
    return len(pairs)


# ---- Parquet 只追加层 ----

def store_signals_raw(rows, dt: str):
    """幂等键 = `observation_key` = `signal_id|ingested_dt`。

    §10.1 的 first-write-wins 说的是「raw_content 首次写入后不可覆盖」。
    但**跨期重复观察不是「覆盖」也不是「重复」**——同一条目在第 1 天和第 3 天各被观察到
    一次，本身就是时间序列信息。故幂等键取「条目 × 采集期」：
      - 同一天重跑 → 幂等（PIT 生效）
      - 换一天再观察到 → 新增一条观察记录（不同 key）
    """
    return append_first_write_wins(SIGNALS_RAW_DIR, dt, rows, key="observation_key",
                                   numeric_cols=("metric_value",))


def store_series_sample(row, dt: str):
    """每源每期的聚合值。幂等键 = `sample_key` = `source_id|ingested_dt`。

    为什么必须有这个对象：§14.6 W3 要「该来源日频序列取对数后的标准差 s_hat」，
    §2.2 的阶段门禁要「该来源累计采样期数」——**两者都要一个期级序列**，
    而 §12 的建库清单此前没有任何对象承载它。只靠 `signals_raw` 的条目，
    「本期 0 条新内容」这件事在库里不留痕迹，而 0 恰恰是最有信息量的取值。
    """
    return append_first_write_wins(BASELINE_SERIES_DIR, dt, [row], key="sample_key",
                                   numeric_cols=("m_items", "m_metric_sum", "m_metric_median",
                                                 "m_metric_max"))


def store_rejection_case(row, dt: str):
    return append_first_write_wins(REJECTION_CASES_DIR, dt, [row], key="case_id")


def load_all_signals_raw():
    return read_all(SIGNALS_RAW_DIR)


def load_all_series():
    return read_all(BASELINE_SERIES_DIR)


def series_index() -> list:
    """期级序列的 `(source_id, ingested_dt)` 逐行投影 —— **只取数，不做任何判定**。

    为什么把「采样期数」的**算法**移出本模块（第 16 次补丁，`[H31]`）：
    期的长度由 `config/sources.json` 的 `source_period` 决定，属 §2.2 的**门禁语义**，
    不是存储层的事；而旧实现（本模块内、无参数、纯 `groupby(ingested_dt).nunique()`）
    **隐含写死了「1 期 = 1 个采集日」**，对 `weekly`（周期型）源会把 1 个采集周数成 7 期
    → 门禁提前跨阶段，且不报错。

    ⚠️ 移出而非原地修补的第二个理由：本函数读磁盘，故**无法对合成输入断言**；
    算法移入 `src/rules/gates.py` 后成为**纯函数**，组 [20] 才能用合成数据证明
    「同一批采集日在 `weekly` 源上只算 1 期」——**改数据才能测 = 测不动 = 不会被测**。
    """
    df = load_all_series()
    if df.empty:
        return []
    return list(zip(df["source_id"].astype(str), df["ingested_dt"].astype(str)))


def item_counts_by_source() -> dict:
    df = load_all_signals_raw()
    if df.empty:
        return {}
    sid = df["signal_id"].astype(str).str.split(":").str[0]
    return sid.value_counts().to_dict()


# ---- §12.1 一致性校验：signals_state vs audit_events 重放 ----

def replay_states(conn) -> dict:
    """从 audit_events 重放出每个 signal 的末态（按 at 排序，末条胜）。"""
    cur = conn.execute(
        "SELECT signal_id, to_status, at FROM audit_events"
        " WHERE signal_id IS NOT NULL ORDER BY at, rowid")
    out = {}
    for row in cur:
        out[row["signal_id"]] = row["to_status"]
    return out


def check_state_consistency(conn) -> dict:
    """§12.1 / §13.2：不一致时以 `audit_events` 为准并告警，报告作废。"""
    replayed = replay_states(conn)
    cur = conn.execute("SELECT signal_id, status FROM signals_state")
    materialized = {row["signal_id"]: row["status"] for row in cur}
    mismatched = [
        {"signal_id": sid, "materialized": materialized.get(sid), "replayed": replayed.get(sid)}
        for sid in sorted(set(replayed) | set(materialized))
        if materialized.get(sid) != replayed.get(sid)
    ]
    return {
        "authority": "audit_events",
        "checked": len(materialized),
        "mismatched": mismatched,
        "ok": not mismatched,
    }
