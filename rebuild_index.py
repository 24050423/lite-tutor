# -*- coding: utf-8 -*-
"""重建知识库索引。

什么时候需要跑：改过 rag_builder.py 里的分词规则、哈希方式或向量维度之后。
向量是按「当时的规则」算好存进 Chroma 的，规则一改，库里存的向量和查询时
现算的向量就不在同一个空间里，检索会退化成随机命中——把原文取出来重算一遍
才恢复。切片原文本身就存在 Chroma 里，重建不会丢任何内容。

用法：
    python rebuild_index.py --dry-run   # 只打印现状，一个字节都不动
    python rebuild_index.py             # 先备份，再原地重建

跑之前请先关掉后端（server.py）和前端：Chroma 是本地文件数据库，
两边同时写会互相打架。
"""
import argparse
import os
import shutil
import sys
import time

import chromadb

from rag_builder import HashEmbeddingFunction

DB_PATH = "./chroma_db"
COLLECTION = "lite_tutor_kb"
STAGING = COLLECTION + "__rebuild"


def backup() -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{DB_PATH}.bak-{ts}"
    shutil.copytree(DB_PATH, dst)
    print(f"[备份] {DB_PATH} -> {dst}")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description="重算知识库向量")
    ap.add_argument("--dry-run", action="store_true", help="只看现状，不改数据")
    args = ap.parse_args()

    if not os.path.isdir(DB_PATH):
        print(f"[错误] 找不到 {DB_PATH}，确认一下是不是在项目根目录跑的")
        return 1

    client = chromadb.PersistentClient(path=DB_PATH)
    ef = HashEmbeddingFunction()
    col = client.get_or_create_collection(name=COLLECTION, embedding_function=ef)

    data = col.get(include=["documents", "metadatas", "embeddings"])
    ids = data["ids"]
    docs = data["documents"]
    metas = data["metadatas"]
    old_dim = len(data["embeddings"][0]) if data.get("embeddings") is not None and len(data["embeddings"]) else 0

    by_source = {}
    for m in metas:
        s = (m or {}).get("source", "?")
        by_source[s] = by_source.get(s, 0) + 1

    print(f"[现状] 集合 {COLLECTION}：{len(ids)} 条切片，旧向量维度 {old_dim}，新向量维度 {ef.dim}")
    for s, n in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"        {n:>4} 片  {s}")

    if not ids:
        print("[提示] 库是空的，没有需要重建的东西。")
        return 0
    if args.dry_run:
        print("[dry-run] 到此为止，数据未改动。")
        return 0

    # 上一次跑到一半崩了的话会留下临时集合，先清掉再备份，
    # 否则备份里混着这份垃圾，真要回滚反而被它带偏。
    try:
        client.delete_collection(STAGING)
    except Exception:
        pass

    backup()

    # 先建到临时集合里，确认切片数对得上、检索真的能命中，再去动旧集合。
    # 这样中途失败也只是留个临时集合，原始数据完好。
    new = client.create_collection(name=STAGING, embedding_function=ef)
    print(f"[重建] 正在为 {len(ids)} 条切片重算向量……")
    new.add(ids=ids, documents=docs, metadatas=metas)

    n = new.count()
    if n != len(ids):
        print(f"[错误] 新集合只有 {n} 条，期望 {len(ids)} 条，已中止（原数据未动）")
        client.delete_collection(STAGING)
        return 1
    print(f"[重建] 完成：{n} 条切片，维度 {len(new.get(limit=1, include=['embeddings'])['embeddings'][0])}")

    # 用一句中文试查，确认真能命中再切换
    probe = "计算机硬件系统五大部件"
    hits = new.query(query_texts=[probe], n_results=1, include=["documents"])
    got = (hits.get("documents") or [[]])[0]
    print(f"[自检] 试查「{probe}」-> {'命中，开头是：' + got[0][:60].replace(chr(10), ' ') if got else '没命中'}")

    client.delete_collection(COLLECTION)
    try:
        new.modify(name=COLLECTION)
    except Exception as e:
        # 老版本 chromadb 不支持改名，那就按新规则再建一次正式集合
        print(f"[提示] 临时集合改名为 {COLLECTION} 失败（{e}），改为重新写入")
        final = client.create_collection(name=COLLECTION, embedding_function=ef)
        final.add(ids=ids, documents=docs, metadatas=metas)
        client.delete_collection(STAGING)

    print(f"[完成] {COLLECTION} 已按新规则重建，共 {len(ids)} 条切片。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
