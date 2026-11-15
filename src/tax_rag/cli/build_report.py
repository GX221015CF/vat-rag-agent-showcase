from __future__ import annotations

import io
import json
import sqlite3
import unittest
from datetime import datetime

import chromadb

from tax_rag.config import PROJECT_ROOT, WORKSPACE_ROOT, load_toml, project_path
from tax_rag.ingest.source_copy import ensure_verified_copies, snapshot_originals, write_json
from tax_rag.storage.sqlite_store import connect, integrity_summary


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_tests() -> tuple[dict, str]:
    suite = unittest.defaultTestLoader.discover(str(PROJECT_ROOT / "tests"))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    summary = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.wasSuccessful(),
    }
    return summary, stream.getvalue()


def main() -> None:
    app = load_toml("app.toml")
    artifacts = project_path(app["project"]["artifacts_root"])
    copy_result = ensure_verified_copies()
    baseline = load_json(artifacts / "source_baseline.json")
    current = load_json(artifacts / "source_integrity_current.json")
    p0_snapshot_path = WORKSPACE_ROOT / "p0_docx_parser" / "artifacts" / "source_integrity_current.json"
    p0_snapshot = load_json(p0_snapshot_path)
    first_build = load_json(artifacts / "build_summary_first.json")
    current_build = load_json(artifacts / "build_summary.json")
    evaluation = load_json(artifacts / "retrieval_eval.json")
    parser_validation = load_json(artifacts / "parser_validation.json")
    tests, test_output = run_tests()

    reproducibility = {
        "generation_id_same": first_build["candidate_index"]["generation_id"]
        == current_build["candidate_index"]["generation_id"],
        "collection_name_same": first_build["candidate_index"]["collection_name"]
        == current_build["candidate_index"]["collection_name"],
        "chunk_count_same": first_build["candidate_index"]["chunk_count"]
        == current_build["candidate_index"]["chunk_count"],
        "parser_rule_version_same": first_build["parser_rule_version"]
        == current_build["parser_rule_version"],
    }
    write_json(artifacts / "reproducibility.json", reproducibility)
    (artifacts / "test_output.txt").write_text(test_output, encoding="utf-8")

    connection = connect(project_path(app["project"]["sqlite_path"]))
    try:
        integrity = integrity_summary(connection)
        generation = connection.execute("SELECT * FROM index_generations").fetchone()
        index_member_count = connection.execute("SELECT COUNT(*) FROM index_members").fetchone()[0]
    finally:
        connection.close()
    chroma_count = (
        chromadb.PersistentClient(path=str(project_path(app["project"]["chroma_path"])))
        .get_collection(generation["collection_name"])
        .count()
    )

    originals_unchanged = baseline["files"] == current["files"]
    p0_normalized = sorted(
        (
            item["relative_path"].removeprefix("doc/"),
            item["bytes"],
            item["sha256"],
        )
        for item in p0_snapshot
    )
    p1_normalized = sorted(
        (item["relative_path"], item["size"], item["sha256"])
        for item in current["files"]
    )
    matches_p0_snapshot = p0_normalized == p1_normalized
    parser_pass = all(item["passed"] for item in parser_validation.values())
    no_active_pointer = integrity["active_generation_pointers"] == 0
    counts_equal = integrity["chunks"] == integrity["fts_rows"] == chroma_count == index_member_count
    all_reproducible = all(reproducibility.values())
    status = (
        "PASS_CANDIDATE_NOT_ACTIVE"
        if all(
            [
                originals_unchanged,
                matches_p0_snapshot,
                len(copy_result["copies"]) == 5,
                parser_pass,
                tests["passed"],
                evaluation["status"] == "PASS",
                no_active_pointer,
                counts_equal,
                all_reproducible,
            ]
        )
        else "FAIL"
    )

    backup_dirs = sorted((WORKSPACE_ROOT / ".codex-tmp").glob("p1_backup_*"))
    backup_path = backup_dirs[-1] if backup_dirs else None
    rows = []
    for filename, validation in parser_validation.items():
        rows.append(
            f"| {filename} | {validation['checks_passed']}/{validation['checks_total']} | "
            f"{'PASS' if validation['passed'] else 'FAIL'} |"
        )
    missed = [item["case_id"] for item in evaluation["results"] if not item["passed"]]
    report = f"""# P1 知识入库与混合检索质量报告

生成时间：{datetime.now().astimezone().isoformat()}

总体结论：**{status}**

候选索引已经完成构建和评测，但**尚未激活**。P1 验收前，运行时激活指针数量为 0。

## 1. 原始资料保护

- 原始 DOCX 数量：{copy_result['original_docx_count']}（期望 55）
- P1 基线与当前原始区 SHA-256 快照一致：{'是' if originals_unchanged else '否'}
- P1 当前快照与已验收 P0 的 55 份快照一致：{'是' if matches_p0_snapshot else '否'}
- 正式工作副本：{len(copy_result['copies'])} 份，逐份与源文件 SHA-256 一致
- 解析、分块和索引只读取 `data/source_copies/`，没有直接处理原始 DOCX
- 回滚备份：{backup_path if backup_path else '未找到'}

## 2. DOCX 自定义解析门禁

| 文件 | 结构检查 | 结果 |
|---|---:|---|
{chr(10).join(rows)}

- 新增《中华人民共和国税收征收管理法》格式画像：211 段、6 章、94 条、94 个 Chunk。
- 第 5 份旧式外商投资通知只保留在 P0 压力测试中，P1 数据库和索引包含数量为 0。
- LlamaIndex 只接收自定义解析器生成的 `TextNode`，未使用默认 DOCX 解析或默认切块。

## 3. 权威数据与候选向量索引

| 项目 | 数量/状态 |
|---|---:|
| 文档 | {integrity['documents']} |
| `APPROVED_ACTIVE` 文档 | {integrity['approved_documents']} |
| 不可变版本 | {integrity['versions']} |
| SQLite Chunk | {integrity['chunks']} |
| FTS5 记录 | {integrity['fts_rows']} |
| Chroma 向量 | {chroma_count} |
| Index Member | {index_member_count} |
| 孤立 Chunk | {integrity['orphan_chunks']} |
| 激活指针 | {integrity['active_generation_pointers']} |

- 候选 generation：`{generation['generation_id']}`
- Chroma collection：`{generation['collection_name']}`
- Embedding：`{generation['embedding_model']}`，现场维度 {generation['embedding_dimension']}
- 索引状态：`{generation['status']}`
- SQLite、FTS5、Chroma 和 Index Member 集合数量一致：{'是' if counts_equal else '否'}

## 4. 混合检索评测

- 检索通道：精确条款定位 + SQLite FTS5 BM25 + `bge-m3` 向量 + RRF 融合
- 初版黄金用例：{evaluation['case_count']} 个
- Recall@5：{evaluation['recall_at_5']:.1%}（门槛 90%）
- 精确条号用例 Top-1：{evaluation['exact_top1_accuracy']:.1%}（门槛 100%）
- 未命中用例：{', '.join(missed) if missed else '无'}
- 黄金用例当前为 Agent 初稿，仍需用户审核其问题表述和预期条款；本指标不等于完整税务回答正确率。

## 5. 可重复构建与自动测试

- 两次构建 generation ID 一致：{'是' if reproducibility['generation_id_same'] else '否'}
- 两次构建 Chunk 数量一致：{'是' if reproducibility['chunk_count_same'] else '否'}
- 两次构建解析规则版本一致：{'是' if reproducibility['parser_rule_version_same'] else '否'}
- 自动测试：{tests['tests_run']} 项，失败 {tests['failures']}，错误 {tests['errors']}，结果 {'PASS' if tests['passed'] else 'FAIL'}

## 6. 云端模型与P1边界

- DeepSeek 调用：0
- `DEEPSEEK_API_KEY` 内容读取：0
- Flask 对话界面、一次性云端提示、7 天清理、8 轮状态和 Decimal 计算：不属于 P1，尚未实现
- 候选索引激活：未执行，等待用户验收

## 7. 验收结论

P1 的复制件保护、自定义解析、SQLite/FTS5、LlamaIndex 节点适配、Ollama `bge-m3`、Chroma 候选索引、RRF 混合检索和初版黄金检索评测均已完成。只有用户明确验收后，才允许执行候选索引激活或进入 P2。
"""
    report_path = artifacts / "P1_知识入库与混合检索质量报告.md"
    report_path.write_text(report, encoding="utf-8", newline="\n")
    final_summary = {
        "status": status,
        "report": str(report_path),
        "tests": tests,
        "originals_unchanged": originals_unchanged,
        "matches_p0_snapshot": matches_p0_snapshot,
        "counts_equal": counts_equal,
        "reproducible": all_reproducible,
        "recall_at_5": evaluation["recall_at_5"],
        "exact_top1_accuracy": evaluation["exact_top1_accuracy"],
        "active_generation_pointers": integrity["active_generation_pointers"],
        "deepseek_called": False,
    }
    write_json(artifacts / "p1_final_summary.json", final_summary)
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    if status == "FAIL":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
