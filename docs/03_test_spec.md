# Test Specification — SVC-C2-033 TourPlanningProcurementRoutingAgent

## 1. Test Strategy

- **Unit**: 各ノード（pre_process/post_process/内側6ノード/main GraphNode）とサービス
  （KB/Supplier/MarketData/PlanDraft LLM）を fake 注入で単体検証。
- **Integration**: 外側グラフを checkpointer 付きで compile し end-to-end 実行（HITL suspend・
  S-2 consent 拒否・S-1 経路）。
- **Proof-of-Boundary**: フレームワーク境界（import 分離・State 安全性・HITL interrupt 伝播）。
- LLM / DB / HTTP は **fake 注入シーム**で置換し、ネットワーク・外部依存なしで実行。

## 2. Framework Compliance Tests (TC)

| TC-ID | Test | 実装 | Expected |
|-------|------|------|----------|
| TC-01 | State は flat TypedDict（Pydantic/dataclass 無し）| `tests/proof_of_boundary/test_state_safety.py` | PASS（禁止型・credential フィールド無し）|
| TC-02 | S-2 で不正入力に status=error | `test_nodes.py::TestPreProcess::test_s2_gate_*` | PASS |
| TC-03 | JWT/Credential が src に無い | `gate-credential-scan` CI | PASS |
| TC-04 | InvocationContext は configurable 経由のみ（State 非格納）| `test_state_safety.py` + 設計不変条件 | PASS |
| TC-05 | S-4 emit_trace_event が各 execute で発火 | 各ノードに emit_trace_event 実装 | PASS |
| TC-06 | `_security_gate_input` は @final・拡張は `_extra_*` | pre_process が `_extra_security_gate_input` のみ override | PASS |
| TC-07 | `_security_gate_output` は @final・拡張は `_extra_*` | post_process が `_extra_security_gate_output` のみ override | PASS |
| TC-08 | `required_trust_level` 宣言・強制 | config `VERIFIED_EXTERNAL`・`test_graph_e2e` S-1 経路 | PASS |

## 3. Proof-of-Boundary Tests (PB)

| PB-ID | Boundary | 実装 | Expected |
|-------|----------|------|----------|
| PB-1 | BaseNode → AuditLogger | 各ノードの emit_trace_event | 発火（silent failure 無し）|
| PB-2 | State serialization | `test_state_safety.py` | primitives のみ |
| PB-4 | Import isolation | `test_import_isolation.py` | L0 import 0 件 |
| PB-5 | Checkpoint safety（センシティブ非格納）| `test_hitl_interrupt.py::test_no_sensitive_values_in_output_before_gate` | 連絡先・仕入条件が suspend 時 output に無い |
| PB-7 | HITL interrupt 伝播 | `test_hitl_interrupt.py::test_interrupt_suspends_not_errors` | status=AWAITING_HUMAN（error にしない）|

## 4. Domain Tests

| ID | Test | 実装 |
|----|------|------|
| D-01 | KB シグナルは非センシティブ集約のみ | `test_nodes.py::test_kb_load_signals` |
| D-02 | 観光API 失敗時 graceful degradation（market_data={}）| `test_nodes.py::test_market_fetch_degrades` |
| D-03 | plan_draft は hitl_draft に idempotency 保存 | `test_nodes.py::test_plan_draft` |
| D-04 | supplier_match_refs に連絡先・条件を含めない | `test_nodes.py::test_supplier_match_refs_have_no_sensitive` |
| D-05 | procurement_refs は参照 ID のみ | `test_nodes.py::test_procurement_refs_only_ids` |
| D-06 | document_generate はセンシティブ resolve を S-3 に委譲 | `test_nodes.py::test_document_generate_defers_sensitive` |
| D-07 | **S-3 ゲートでのみ**センシティブ値を resolve | `test_nodes.py::TestPostProcessS3::test_s3_resolves_sensitive_only_in_output` |
| D-08 | main GraphNode の HITL/error フラグ・merge に HITL フィールド | `test_main_node.py` |
| D-09 | e2e: HITL suspend / consent 拒否 | `test_graph_e2e.py` |

## 5. Execution

```bash
source .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest tests/ -v
python -m pytest tests/proof_of_boundary/ -v
```

現状: **25 tests PASS**（unit + integration + PB）。ruff clean。全 CI ゲート緑（ローカル検証）。

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | Azure OpenAI 上の GPT モデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['type', 'reason'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 LLM の鍵が要るため）。
