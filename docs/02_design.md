# Template Design Specification — SVC-C2-033 TourPlanningProcurementRoutingAgent

## Position in AgentCore Architecture

- **Agent Class**: `Graph`（`src/graph/graph.py`、外側グラフ）
- **L1 Base**: **AgentBaseGraph**（Cat 2 = 外側 AgentBaseGraph ＋ `main` スロットに `GraphNode` を配置し
  内側ドメインワークフローグラフをラップ）。旧 Level 2 base 継承モデルは廃止され、本テンプレは
  **L1 フレームワークを直接継承**する（`AutonomousBaseGraph` は使用しない）。
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不可 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state: dict) -> dict` のみ override）
  - Graph: composition（`register_nodes()` によるノード差し替え、`add_edges()` は override しない）

## Architecture Overview

Cat 2 の 2 層構成:

- **外側グラフ**（`src/graph/graph.py`・`Graph(AgentBaseGraph)`）: 固定 5 ノード backbone。
  ドメイン複雑性は `main` スロットの `TourPlanningGraphNode`（`GraphNode` サブクラス）に隠蔽する。
  `add_edges()` は override しない。
- **内側グラフ**（`src/graph/domain_workflow_graph.py`・`TourPlanningWorkflow(BaseGraph)`）: ツアー企画の
  多段ドメインロジックを独自トポロジで実装。**企画ドラフト後に HITL（`interrupt()`）を挟み**、人間の
  承認・修正を反映してから調達動線生成へ進む。

### ユーザー体験（使い勝手）を起点にしたデータ設計

旅行会社の現実（travelvoice 2026: 調達は Excel・紙・担当者の記憶で運用）を前提に、**「構造化KBを
持っていない顧客でも使える」** ことを最優先にする。社内データは **Excel/CSV アップロードを第一級サポート**
とし、AG 側の軽量マッパーが正規化スキーマへ ingest する。顧客に DB 構築を要求しない（＝自治体案で
撤回した障壁を旅行会社案でも作らない）。

#### 社内KBデータの投入形式と落とし先（第一級サポート = Excel/CSV）

| データ種別 | ユーザー投入（Excel/CSV 列） | 落とし先テーブル | sensitive 列 |
|---|---|---|---|
| 過去ツアー実績・販売データ | tour_name / period / theme / area / price_band / units_sold / gross_margin / rating | `past_tours` | なし（社内実績のみ・PII 非該当） |
| サプライヤーマスタ | supplier_name / category / area / **contact** / **standard_terms** / lead_time_days | `suppliers` | `contact`, `standard_terms`（**S-3 センシティブ**） |

- **DB クライアント = `shared.services.database.base_db.BaseDatabase`**（SDK 同梱・`postgres_client` /
  `mysql_client`）。顧客ごとに **テナント分離**（接続情報は `ctx.secrets.require()` で取得、State 非格納）。
- **ingest マッパー**（`KBService.ingest_csv()` / `SupplierService.ingest_csv()`）: Excel/CSV の列を
  上記正規化スキーマへマッピングして UPSERT。列名ゆらぎは alias 表で吸収。ingest 自体は本 AG の
  invoke フローとは別の運用ジョブ（手順は運用ガイドに記載）。invoke 時は
  正規化済テーブルを read するだけ。
- **sensitive 列の扱い**: `suppliers.contact` / `suppliers.standard_terms` は S-3 センシティブ。
  内側ワークフローでは **参照 ID（supplier_id）だけを State に載せ**、実値は service handle に保持。
  最終出力（S-3 ゲート通過後）でのみ resolve して展開する。

#### 参照テーブル DDL（正規化スキーマ・実装者向け）

```sql
CREATE TABLE past_tours (
  tour_id       TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  tour_name     TEXT NOT NULL,
  period        TEXT,             -- 例: "2025-autumn"
  theme         TEXT,
  area          TEXT,
  price_band    TEXT,             -- 例: "80k-120k"
  units_sold    INTEGER,
  gross_margin  NUMERIC,
  rating        NUMERIC
);

CREATE TABLE suppliers (
  supplier_id     TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  supplier_name   TEXT NOT NULL,
  category        TEXT,           -- 宿泊 / 交通 / 体験 / 飲食 等
  area            TEXT,
  contact         TEXT,           -- ★ S-3 sensitive（State 非格納）
  standard_terms  TEXT,           -- ★ S-3 sensitive（State 非格納）
  lead_time_days  INTEGER
);
```

### Node Configuration（外側 backbone）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema_version / session_id / trust_level 設定 | user_input | schema_version, session_id, caller_trust_level | InitializeNode (default) |
| pre_process | 入力検証・正規化（segment/season/theme/area/budget_band の抽出）、S-2 拡張入力ゲート（PII・consent・入力サイズ） | user_input | validated_input, planning_request | PreProcessNode（`FunctionNode`、`_extra_security_gate_input()` 拡張） |
| main | 内側ワークフローを GraphNode 経由で実行し結果を outer state へ merge。上流 error パススルー。**内側 HITL を外側へ伝播（`propagate_hitl=True`）**。`merge_output` は HITL フィールドも戻す（下記） | validated_input | plan_draft, procurement_refs, result, status, **hitl_draft, hitl_feedback, hitl_status** | TourPlanningGraphNode（`GraphNode`） |
| post_process | 出力整形（企画ドラフト＋調達アクションリスト・**参照 ID のみ**）、**S-3 拡張出力ゲート＝安全検査**（センシティブキー混入の防御的拒否＋LLM 生成文の PII 再走査）。**センシティブ実値の resolve はここで行わない**（post_process の戻り値も checkpoint されるため）| result | formatted_output | PostProcessNode（`FunctionNode`、`_extra_security_gate_output()` 拡張） |
| finalize | response_metadata / total_time_ms 構築、最終 `output` 射影 | formatted_output | response_metadata, output | FinalizeNode (default) |

### Node Configuration（内側 `TourPlanningWorkflow`・BaseGraph 独自トポロジ・HITL 入り）

| Node | Responsibility | 主な出力 State キー |
|------|---------------|-------------|
| internal_kb_load | `KBService` 経由で `past_tours` から勝ちパターン要約を抽出（非センシティブ）。生レコードは State 非格納。S-4 `emit_trace_event("kb_load", ...)` | kb_signals |
| market_data_fetch | **東京都観光オープンデータ API** を叩き季節/イベント/エリア情報を取得（`MarketDataService`・データソースは config 差し替え可能なシーム。既定=東京都、顧客提供 URL / 他自治体 API へ切替可）。**失敗時 degradation（L-02）: API 障害・レート制限・タイムアウト時は止めずに `market_data: {}` で続行（graceful degradation）＋ `emit_trace_event("market_fetch_degraded", ...)`**。`market_data` が空の場合、plan_draft は社内KB シグナル（kb_signals）のみで起案し、プロンプトに「外部観光データ取得不可＝一般知識で補完せず社内実績ベースで起案」と明示（捏造抑止）。S-4 `emit_trace_event("market_fetch", ...)` | market_data |
| plan_draft | LLM（`shared.services.llm.base_llm.BaseLLM`）が kb_signals＋market_data に **grounding** してツアー企画ドラフト（concept/itinerary/differentiation/target）を起案。取得データにない固有名詞を使わない指示で捏造抑止。S-4 `emit_trace_event("llm_plan_draft", ...)` | plan_draft, hitl_draft |
| **hitl_review**（HITL） | 企画ドラフトを人間に提示し `interrupt({...})` で承認 or 修正フィードバックを受領。承認/修正を反映してから後段（調達）へ進む。**未承認の企画で調達連絡先を確定しない安全順序**。resume 時は `hitl_draft` で LLM 再実行を防止 | plan_draft（修正反映）, hitl_feedback, hitl_status |
| supplier_match | 必要資材を列挙し `SupplierService` 経由で `suppliers` と突合。連絡先・仕入条件は State 非格納、**supplier_id 参照＋突合可否のみ** State に載せる | required_materials, supplier_match_refs |
| procurement_route | 調達アクションを生成。センシティブ実値（連絡先・仕入条件・仮押さえ）は **State に一切載せず**、State には **参照 ID（supplier_id / action_id）のみ** | procurement_refs |
| document_generate | 企画ドラフト＋調達動線の**骨子（参照 ID ベース・センシティブ実値なし）**を組み立てる。**センシティブ実値は resolve しない**（resolve は post_process の S-3 ゲート内でのみ実施）。State の `result` には参照 ID とプレースホルダのみ格納 | result（参照のみ）, status |

> ⚠️ **重要（S-3 漏洩防止の要・checkpoint 実機を踏まえた確定方針）**:
> checkpointer を付けると **LangGraph は super-step ごとに State を永続化する**（HITL suspend 中も完了後も）。
> したがって **グラフ内のどのノードの戻り値にもセンシティブ実値を載せてはならない**（post_process も含む・
> post_process の戻り値も checkpoint される）。
> - `document_generate` / post_process は **参照 ID とプレースホルダのみ**を `result` / `formatted_output` に持つ。
> - post_process の `_extra_security_gate_output()` は **安全検査**（センシティブキー混入の防御的拒否＋
>   LLM 生成文の PII 再走査）を担い、**resolve はしない**。
> - **センシティブ実値の resolve は checkpoint されない場所＝API アダプタ（`src/api/server.py`）で
>   `invoke()` の戻り値に対してのみ行う**（`src/services/output_resolver.py`）。呼び出し元へ返る
>   レスポンスにだけ連絡先・条件が現れ、checkpoint DB には決して入らない。
> - **不変条件**: センシティブ実値が checkpoint される経路をグラフ内に一切作らない。

### Data Flow

外側 backbone（固定）:

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

`main`（TourPlanningGraphNode）内で実行される内側トポロジ（HITL 入り）:

```
START → internal_kb_load → market_data_fetch → plan_draft
          → hitl_review 【interrupt: 企画ドラフトを人間に提示・修正受付】
          → supplier_match → procurement_route → document_generate → END
```

#### route() の振る舞い（M-02: HITL 割り込み時を含む）

外側 `route()`（`AgentBaseGraph` 既定・**override しない**）は `main` の後に `state["status"]` を読む:

| main 後の status | route() 遷移 | 備考 |
|---|---|---|
| `success` | → post_process | 正常完了（HITL 承認済みで最後まで走った） |
| `retry` | → pre_process | リトライ（max 3） |
| `error` | → finalize | 上流エラーの fail-fast |
| `awaiting_human`（HITL 割り込み） | route() は呼ばれない | **`main`（GraphNode）が `lg_interrupt` を発火し、外側 `invoke()` が `_build_awaiting_human_output` を返して即座に suspend する（route() 到達前）**。resume 時は checkpoint から `main` を再開し、承認完了後に `success` を返して post_process へ進む |

- `route()` は既定の 3 分岐（success/retry/それ以外→finalize）のまま。**HITL は route() の分岐ではなく
  `interrupt()` による graph suspend で表現される**（framework が `__interrupt__` を返し `invoke()` が
  RunnableConfig 経由で checkpoint する）。実装者は route() に `awaiting_human` 分岐を足す必要はない。
- `pre_process → main` は無条件辺のため、`main`（GraphNode）は上流の `status="error"` を必ずパススルーする
  （`GraphNode.execute` を override して上流 error 時 `return {}`）。
- **HITL 伝播**: 内側 hitl_review の `interrupt()` を外側呼び出し元へ届けるため `propagate_hitl=True`。
  inner/outer 両グラフを **明示的に `compile(checkpointer=...)`** する（詳細は下記 §HITL Compliance H-01）。

### State Definition

flat TypedDict（`State(AgentState)`、`src/schemas/state.py`）。HITL 7 フィールドは AgentState 継承で入る。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| user_input | str | 生入力（segment/season/theme/area/budget_band を含む自然文または JSON） | Yes |
| validated_input | str | 検証・正規化後の入力 | No |
| planning_request | dict | pre_process が構築する構造化リクエスト。**キー（L-01 確定）**: `segment: str`（顧客層）/ `season: str`（例 "2026-spring"）/ `theme: str` / `area: str` / `budget_band: str`（例 "80k-120k"）/ `consent: bool`（S-2 で検証）。inner_kb_load はこのキー集合をそのまま受け取る | No |
| kb_signals | dict | 社内KBから抽出した**非センシティブな**要約シグナル（勝ちパターン特徴量・実績カウント等）。生レコード・仕入条件は含めない | No |
| market_data | dict | 東京都観光API 等から取得した季節/イベント/エリア情報（公開情報） | No |
| plan_draft | dict | ツアー企画ドラフト（concept/itinerary/differentiation/target） | No |
| required_materials | list | 必要資材の列挙（品目名のみ・非センシティブ） | No |
| supplier_match_refs | list | サプライヤー突合結果の**参照ID（supplier_id）＋突合可否**（連絡先・仕入条件は含めない） | No |
| procurement_refs | list | 調達アクションの**参照ID**（センシティブ値は最終出力にのみ展開） | No |
| result | dict | 企画ドラフト＋調達動線の**骨子（参照 ID＋プレースホルダのみ・センシティブ実値なし）**。post_process の S-3 ゲートで resolve される前段データ | No |
| formatted_output | dict | post_process 整形後の最終出力（finalize が `output` へ射影） | No |
| status | str | AgentStatus 値（"success"/"error"/"retry"） | Yes |
| hitl_draft | str\|dict | 企画ドラフトの idempotency ガード（resume 時 LLM 再実行防止） | No（HITL 時） |
| hitl_feedback | str\|dict | 人間の承認/修正フィードバック（framework が resume 時に書込） | No（HITL 時） |
| hitl_status | HitlStatus | HITL の意味的結果（framework 書込） | No（HITL 時） |

> **State センシティブ情報分離（重要）**: サプライヤーの連絡先・仕入条件・仮押さえ内容は S-3 センシティブ情報で
> あり、**checkpoint される State には格納しない**。生KB レコード／サプライヤーマスタは `KBService` /
> `SupplierService` の service handle（transient・リクエストスコープ）に保持し、State には非センシティブな
> シグナル・参照 ID・突合可否のみを載せる。センシティブな実値は post_process の S-3 出力ゲートを通過した
> **最終レスポンスにのみ** resolve して展開する。これにより LangGraph checkpoint DB へのセンシティブ情報漏洩を防ぐ。

**State Constraints (mandatory):**
- Flat TypedDict のみ（primitives + JSON-serializable 型）
- JWT・API キー・credential を State に格納しない（checkpoint DB 漏洩）
- InvocationContext は `config["configurable"]` 経由のみ（State に入れない）
- Pydantic モデル・dataclass・任意 Python オブジェクトを格納しない（msgpack 非互換）
- secrets は State に入れず `ctx.secrets.require()`（DB接続情報・観光API キー等）で取得
- **サプライヤー連絡先・仕入条件・仮押さえ内容などの S-3 センシティブ実値を State に格納しない**

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, permissions, credential handle）
- [x] ConnectionPolicy（社内KB DB・観光API のリトライ／タイムアウト戦略）
- [x] SecurityViolationError
- [x] `shared.services.database.base_db.BaseDatabase`（postgres/mysql・社内KB DB クライアント）
- [x] `shared.services.llm.base_llm.BaseLLM`（`complete(messages)`・plan_draft の LLM）
- [x] `shared.security.pii_detector.detect_pii`（S-2 既定 PII 走査の補強）
- [x] `shared.utils.audit_logger.emit_trace_event`（S-4）
- [x] S-2: `_extra_security_gate_input()` — pre_process で入力側ドメインチェック
      （planning_request 各フィールドの PII 走査・consent フラグ検証・入力サイズ上限）。
      reject 時は `state["status"]=AgentStatus.ERROR.value` + error_log を返す（raise しない）。
      `_security_gate_input()` は override しない（`@final`・class 定義時 TypeError）
- [x] S-3: `_extra_security_gate_output()` — post_process で出力側ドメインチェック
      （調達アクションの仕入条件・連絡先などセンシティブ情報の resolve＋出力ゲート、
      LLM 出力への credential/PII 再走査）。ブロック時は raise 可。
      `_security_gate_output()` は override しない（`@final`）
- [x] S-4: `emit_trace_event()` — 副作用を伴う各ノードの `execute()` 内で最低 1 件のドメインイベント発行
      （kb_load / market_fetch / llm_plan_draft 等）。`node_start`/`node_complete`/`node_error` は
      `BaseNode.__call__()` が自動発行するため execute 内で重複発行しない

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` サブクラス（pre_process/post_process/内側ドメインノード）→ framework `@final` ゲートが自動実行。
>   ドメイン拡張は `_extra_security_gate_input()` / `_extra_security_gate_output()` のみ
> - `GraphNode`（main = TourPlanningGraphNode）→ 意図的 no-op（内側グラフのゲートが適用済）
> - S-3 の中核ゲート（調達センシティブ情報）は **post_process の `_extra_security_gate_output()`** に置く

### HITL Compliance（`hitl.enabled: true`）

- **配置**: 内側 `hitl_review` ノードの `execute()` 内で `interrupt({...})` を呼ぶ（D6 backbone パターン）。
  企画ドラフト（concept/itinerary/differentiation/target）と reason を提示し、resume で人間の承認/修正を受領。
- **順序の安全性**: HITL は `plan_draft` 直後・`supplier_match`（調達動線＝センシティブ）**より前**に置く。
  未承認の企画で仕入先連絡・仮押さえを確定しない。
- **idempotency**: resume 時に `hitl_draft` を参照して LLM を再実行しない。
- **config 必須（ADR-016 D5）**:
  ```yaml
  memory_enabled: true          # checkpointer 必須（pause/resume）
  hitl:
    enabled: true
    max_hitl: 8                  # 企画修正の反復上限
  ```

#### H-01: checkpointer 伝播の具体的経路（1.0.0 wheel 実機確認済み）

> ⚠️ **1.0.0 wheel の実挙動**（`framework.graph.base_graph` / `agent_base_graph` / `nodes.graph_node`
> を introspection して確定）: `BaseGraph.compile()` / `AgentBaseGraph.compile()` は
> **config から checkpointer を自動生成しない**。渡された `checkpointer` 引数をそのまま
> `_build_graph()` → `StateGraph.compile(checkpointer=...)` に流すだけ。かつ lazy path の
> `_ensure_compiled()` は **`self.compile()` を引数なしで呼ぶ**＝checkpointer=None。
> したがって **明示的に `compile(checkpointer=<saver>)` を呼ばないと HITL は動かない**
> （`interrupt()` 発火時に `invoke()` が「interrupt() called but no checkpointer」で RuntimeError）。
> `_parent_config()` は **sample ファイル独自のヘルパーで wheel には存在しない**（サンプルの記述を
> 鵜呑みにしない）。config は inner graph の `BaseGraph.__init__(config=...)` コンストラクタで渡す。

**外側 `Graph`（AgentBaseGraph）— checkpointer attach 経路:**
```python
# src/api/server.py — HITL 用に明示 compile（scaffold の引数なし compile() を置換）
from langgraph.checkpoint.memory import InMemorySaver   # 本番は永続 saver に差替
import yaml
cfg = yaml.safe_load(open("config/agent.yaml"))["agent"]  # memory_enabled / hitl を含む
agent = Graph(config=cfg)
agent.compile(checkpointer=InMemorySaver())               # ★ checkpointer を明示注入
```
- `AgentBaseGraph.invoke()` は `self.config.get("memory_enabled") or self.config.get("hitl",{}).get("enabled")`
  が真のとき thread_config（`{"configurable":{"thread_id":...}}`）を付けて `_compiled.invoke()` する。
  よって **outer の `config` にも `memory_enabled: true` / `hitl.enabled: true` が必要**。

**内側 `TourPlanningWorkflow`（BaseGraph）— checkpointer attach 経路:**
```python
# src/graph/graph.py — TourPlanningGraphNode
class TourPlanningGraphNode(GraphNode):
    propagate_hitl = True
    error_strategy = "propagate"
    def get_subgraph(self):
        # inner は独自に checkpointer 付きで compile 済みインスタンスを返す。
        # GraphNode.execute は subgraph.invoke()/subgraph.resume() を呼ぶだけで
        # checkpointer を渡さない＝inner 自身が compile 時に保持している必要がある。
        inner = TourPlanningWorkflow(config={"memory_enabled": True,
                                             "hitl": {"enabled": True, "max_hitl": 8}})
        inner.compile(checkpointer=InMemorySaver())        # ★ inner にも明示注入
        return inner
```
- **HITL 伝播フロー（wheel 実機）**: 内側 `hitl_review` が `interrupt()` → 内側 `invoke()` が
  `AgentStatus.AWAITING_HUMAN` + `thread_id` を返す → `GraphNode.execute` が `propagate_hitl=True` かつ
  `status==AWAITING_HUMAN` を検知して外側で `lg_interrupt({"subgraph_thread_id":..., **hitl_metadata})` を
  再発火 → 外側 `invoke()` が `_build_awaiting_human_output` を返す。
- **resume フロー**: 外側再開時、`GraphNode.execute` は `state["subgraph_thread_id"]` を見て
  `subgraph.resume(thread_id, feedback)` を呼ぶ（`get_subgraph()` が返す inner は **必ず BaseGraph 継承**で
  `resume()` を持つこと。非 BaseGraph だと AttributeError → `_handle_call_error`）。
- **outer/inner 両方に checkpointer 必須**（片方欠けると欠けた側の interrupt で RuntimeError）。

- **PB-7**: `interrupt()` は `GraphInterrupt` を伝播させ、アプリのエラー境界で捕捉しない（status を error にしない）。

#### M-01: `hitl_review.execute()` 実装スケッチ

```python
from langgraph.types import interrupt
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus

class HitlReviewNode(FunctionNode):
    def execute(self, state: dict) -> dict:
        # idempotency: 既に draft 提示済みなら LLM/前段を再実行しない
        draft = state.get("hitl_draft") or state.get("plan_draft")
        # interrupt ペイロード: 人間に提示する企画ドラフト + 理由
        feedback = interrupt({
            "type": "plan_review",
            "draft": draft,                    # concept/itinerary/differentiation/target
            "reason": "企画ドラフトの承認または修正指示をお願いします",
        })
        # resume 後: feedback（承認 or 修正指示）を反映
        # 承認 → plan_draft をそのまま確定 / 修正 → feedback を plan_draft へマージ
        approved = isinstance(feedback, dict) and feedback.get("approved") is True
        revised = draft if approved else _apply_feedback(draft, feedback)
        return {
            "plan_draft": revised,
            "hitl_draft": draft,               # idempotency ガード（再 interrupt 防止）
            "hitl_feedback": feedback,
            "hitl_status": AgentStatus.SUCCESS.value if approved else "revised",
        }
```
- interrupt ペイロード形式（`type`/`draft`/`reason`）と resume 後の反映ロジック（承認 or 修正マージ）を上記に確定。
- `hitl_count` / `hitl_allowed` はテンプレが書かない（framework/gateway 管理）。

### Composition Pattern

- **Pattern**: GraphNode（subgraph）— `main` スロットに `TourPlanningGraphNode` を配置し
  `src/graph/domain_workflow_graph.py` の `TourPlanningWorkflow`（BaseGraph）をラップ
- **Composition target**: `TourPlanningWorkflow`（内側ドメインワークフロー、HITL 入り）
- **GraphNode の abstract 実装**: `extract_input(state)->str`（planning_request を
  `json.dumps({...}, default=str)` で直列化・内側先頭ノードで parse）/ `get_subgraph()`（上記 H-01 の
  とおり `TourPlanningWorkflow(config={...})` を生成し `compile(checkpointer=...)` 済みで返す。
  ⚠️ `_parent_config()` は wheel 非存在＝使わない）
  / `merge_output(state, sub_result)->dict`。**戻すキー**: `result, status, plan_draft, procurement_refs,
  supplier_match_refs`（ドメイン成果）＋ **HITL フィールド `hitl_draft, hitl_feedback, hitl_status`**
  （`propagate_hitl=True` 時、これらを merge に含めないと resume でドラフト・人間フィードバックが失われ
  D6 の「LLM 再実行しない」保証が壊れる）。変更キーのみ返す（full state 禁止）。
  なお `hitl_count` / `hitl_allowed` は framework/gateway が書くフィールドでテンプレは書かない。
- **Error propagation strategy**: **propagate**（`error_strategy = "propagate"`）— 内側の例外を
  `SubgraphError` として再送出し fail-fast。企画・調達は不完全な部分結果を返すべきでないため handle ではなく propagate を採用

## Import Isolation Confirmation
- [x] Level 0（agenticstar-platform SDK）を import しない
- [x] import 先は framework/ と shared/ のみ（agents/base/ 不要）。LLM は `shared.services.llm.base_llm.BaseLLM`、
      DB は `shared.services.database.base_db.BaseDatabase`、PII は `shared.security.pii_detector`、
      監査は `shared.utils.audit_logger.emit_trace_event` を使用（framework.llm / framework.security は wheel 非提供）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプラインで完結する多段ワークフロー。自律 think→act ループ不要（Cat 2） |
| Composition pattern | Standalone | GraphNode (subgraph) | **GraphNode (subgraph)** | 多段ドメインロジックを内側グラフに隠蔽し外側 backbone を固定に保つ（Cat 2 標準） |
| KB 投入方式 | 顧客に構造化DB構築を要求 | **Excel/CSV 取込を第一級 + テナント分離DB** | **Excel/CSV 取込を第一級** | 旅行会社は構造化KBを持たない現実（Excel/紙/記憶）。取込敷居を下げ「顧客がKBを作れない」障壁を潰す＝使い勝手の核 |
| 観光データソース | ハードコード単一 | **config 差し替えシーム（既定=東京都観光API）** | **差し替えシーム** | AG テンプレとして顧客提供URL/他自治体APIへ切替可能に。既定は東京都観光オープンデータ API |
| HITL | なし（一発生成） | **plan_draft 直後に HITL** | **plan_draft 直後に HITL** | 企画ドラフトは人間が最終判断すべき成果物。承認前に調達連絡先を確定しない安全順序も両立 |
| checkpointer 伝播 | config から自動 attach 前提 | **inner/outer とも明示 `compile(checkpointer=...)`** | **明示 compile** | 1.0.0 wheel 実機確認: compile は config から checkpointer を自動生成しない・lazy `_ensure_compiled` は checkpointer=None。明示注入しないと HITL が RuntimeError（H-01） |
| Error propagation | propagate | handle | **propagate** | 企画・調達は不完全な部分結果を返すべきでない。上流エラーは fail-fast し retry/finalize に委ねる |
| S-3 安全ゲート配置 | main | post_process | **post_process（安全検査のみ）** | post_process の `_extra_security_gate_output()` はセンシティブキー混入拒否＋PII 再走査を担う（resolve はしない）|
| センシティブ実値の resolve 場所 | post_process（グラフ内）| **API アダプタ（invoke 後）** | **API アダプタ** | checkpointer は super-step ごとに State 永続化＝グラフ内ノードの戻り値は全て checkpoint される。post_process で resolve すると連絡先が checkpoint DB に残る（敵対的コードレビューの HIGH 指摘）。resolve は checkpoint されない `server.py` の invoke 戻り値に対してのみ行う（`output_resolver`）|
| センシティブ情報の保持 | State に生KB・仕入条件を格納 | service handle ＋ 参照 ID のみ State | **service handle ＋ 参照 ID のみ State** | 連絡先・仕入条件は S-3 センシティブ情報。checkpoint DB 漏洩防止のため生値は State/グラフ戻り値に一切載せず、アダプタでレスポンスにのみ resolve |
| Dual-mode 回避 | 企画＋調達を別テンプレ | 単一ジョブとして統合 | **統合（単一 Cat 2）** | 企画ドキュメント内の調達動線は単一の商品企画ジョブの一部。Dual-mode ではない |
| Marketplace DB backend | SDK Postgres/MySQL client | **SQLite + backend 注入シーム** | **SQLite** | SDK 1.0.0〜1.0.2 の Postgres/MySQL client は全メソッド未実装。`create_database(..., database=...)` を本番 backend の差し替え点として維持する |
| 動作確認データ | 外部 DB を必須化 | **自己申告型 sample JSON を冪等投入** | **sample JSON** | Marketplace の実動作を再現可能にしつつ、`SAMPLE-*` ID と「（サンプル）」名称で実データとの誤認を防ぐ。設定されたコーパスの欠落・不正は起動時に fail-loud とする。本番では sample path を外して顧客 DB に差し替える |
| KB 利用証跡 | 内部 State のみ | **非センシティブ集計を成果物へ射影** | **`kb_signals` を射影** | 生レコードや連絡先を公開せず、`sample_count` / `top_themes` / `avg_rating` で KB が実際に参照されたことを確認可能にする |
| Marketplace HITL / supplier details | HITL と連絡先解決を有効化 | **HITL 抑止・参照 ID のみ** | **抑止・未解決** | Marketplace runner は `awaiting_human` を失敗扱いにする。(internal issue reference removed) / §2-7a に従い自動経路では HITL を抑止し、任意発呼者へ `contact` / `standard_terms` を開示しない |
