# -*- coding: utf-8 -*-
"""只追加层（Parquet）—— §10.1 PIT first-write-wins 的实现。

按 `dt=YYYY-MM-DD` 分区，每日一个 part 文件。同一 `dt` 内重复运行时，
**已存在的 key 一律保留首次内容**，新 key 才追加。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def part_path(base: Path, dt: str) -> Path:
    d = base / ("dt=%s" % dt)
    d.mkdir(parents=True, exist_ok=True)
    return d / "part-0000.parquet"


def _normalize(df: pd.DataFrame, numeric_cols=()) -> pd.DataFrame:
    """统一列类型。

    必要性：`metric_value` 在不同来源间混有 int 与 None，pandas 会推成 object，
    pyarrow 落盘时报 `Expected bytes, got a 'int' object`。显式归一后类型稳定，
    且 None 落为 Parquet null 而不是字符串 "None"。
    """
    for col in df.columns:
        if col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = df[col].astype("string")
    return df


def append_first_write_wins(base: Path, dt: str, rows, key: str = "signal_id",
                            numeric_cols=()) -> dict:
    """追加写入，已存在 key 的内容**不覆盖**。

    返回 inserted / skipped / inserted_keys / path。
    `inserted_keys` 是本次真正落盘的新 key —— 审计事件只对它们写（PIT 跳过的不是新信号）。
    """
    path = part_path(base, dt)
    incoming = pd.DataFrame(rows)
    if incoming.empty:
        return {"inserted": 0, "skipped": 0, "inserted_keys": [], "path": str(path),
                "reason": "empty_batch"}
    incoming = _normalize(incoming, numeric_cols)

    # 同批次内先按 key 取首条，避免批内重复
    incoming = incoming.drop_duplicates(subset=[key], keep="first")

    if path.exists():
        existing = _normalize(pd.read_parquet(path), numeric_cols)
        seen = set(existing[key].astype(str))
        keep = incoming[~incoming[key].astype(str).isin(seen)]
        skipped = len(incoming) - len(keep)
        if keep.empty:
            return {"inserted": 0, "skipped": skipped, "inserted_keys": [], "path": str(path),
                    "reason": "all_keys_present"}
        combined = pd.concat([existing, keep], ignore_index=True)
    else:
        keep, skipped, combined = incoming, 0, incoming

    combined.to_parquet(path, index=False)
    return {"inserted": len(keep), "skipped": skipped,
            "inserted_keys": [str(k) for k in keep[key]], "path": str(path)}


def read_all(base: Path) -> pd.DataFrame:
    files = sorted(Path(base).rglob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def read_partition(base: Path, dt: str):
    path = part_path(base, dt)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)
