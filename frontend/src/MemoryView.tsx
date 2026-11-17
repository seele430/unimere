import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, Plus, RotateCcw, X } from "lucide-react";

import "./MemoryView.css";

type Category = "all" | "stable" | "project" | "episode";
type MemoryKind = "preference" | "constraint" | "project_fact" | "episode";

type MemoryItem = {
  id: number;
  key: string;
  kind: MemoryKind;
  layer: string;
  text: string;
  trust: string;
  source_ref: string;
  source_session_id: string | null;
  context_usage: "always" | "related" | "manual";
  status: "active" | "deleted";
  valid_from: string;
  valid_to: string | null;
  supersedes_id: number | null;
  created_at: string;
  updated_at?: string;
};

type HistoryRow = MemoryItem & {
  deleted_at: string | null;
  is_current: boolean;
  version_status: string;
};

type History = {
  anchor_id: number;
  current_id: number | null;
  items: HistoryRow[];
  count: number;
  total_count: number;
  truncated: boolean;
  window_has_current: boolean;
};

const CATEGORY_TEXT: Record<Category, string> = {
  all: "全部",
  stable: "偏好与约束",
  project: "项目事实",
  episode: "工作记录",
};

const KIND_TEXT: Record<MemoryKind, string> = {
  preference: "个人偏好",
  constraint: "行为约束",
  project_fact: "项目事实",
  episode: "工作记录",
};

const STATUS_TEXT: Record<string, string> = {
  current: "当前版本",
  superseded: "旧版本",
  expired: "已失效",
  future: "未生效",
  deleted: "已删除",
};

const USAGE_TEXT = {
  always: "新对话自动使用",
  related: "相关时使用",
  manual: "仅留档",
};

function sourceText(sourceRef: string) {
  if (sourceRef.startsWith("chat:")) return "对话捕获";
  if (sourceRef.startsWith("api:")) return "手动管理";
  if (sourceRef.startsWith("run:")) return "平台记录";
  return sourceRef ? "其他来源" : "未记录";
}

function formatTime(value?: string) {
  if (!value) return "未记录";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

export default function MemoryView({
  authToken,
  onOpenSession,
}: {
  authToken?: string;
  onOpenSession?: (sessionId: string) => void;
}) {
  const token = authToken ?? "";
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [mode, setMode] = useState<"list" | "search">("list");
  const [category, setCategory] = useState<Category>("all");
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<MemoryItem | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [creating, setCreating] = useState(false);
  const [newText, setNewText] = useState("");
  const [newKind, setNewKind] = useState<"preference" | "constraint" | "project_fact">("preference");
  const [busy, setBusy] = useState(false);
  const requestRef = useRef(0);
  const detailRef = useRef(0);

  const load = useCallback(async (
    nextMode: "list" | "search",
    nextQuery: string,
    nextOffset: number,
    nextCategory: Category,
  ) => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setLoading(true);
    setError("");

    try {
      const params = new URLSearchParams({
        limit: nextMode === "search" ? "50" : "20",
        category: nextCategory,
      });
      if (nextMode === "search") params.set("query", nextQuery);
      else params.set("offset", String(nextOffset));

      const response = await fetch(`/api/memory/items?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
      if (requestRef.current !== requestId) return;
      if (!response.ok) throw new Error(`加载失败（${response.status}）`);
      const data = await response.json();
      if (requestRef.current !== requestId) return;

      setItems(data.items ?? []);
      setTotal(data.mode === "search" ? data.count ?? 0 : data.total ?? 0);
      setMode(data.mode === "search" ? "search" : "list");
    } catch (cause) {
      if (requestRef.current === requestId) {
        setError(cause instanceof Error ? cause.message : "加载失败");
      }
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load("list", "", 0, "all");
  }, [load]);

  async function openDetail(item: MemoryItem) {
    const requestId = detailRef.current + 1;
    detailRef.current = requestId;
    setDetail(item);
    setHistory(null);
    setHistoryError("");
    setEditing(false);
    setEditText(item.text);

    try {
      const [detailResponse, historyResponse] = await Promise.all([
        fetch(`/api/memory/items/${item.id}`, {
          headers: { Authorization: `Bearer ${token}` }, cache: "no-store",
        }),
        fetch(`/api/memory/items/${item.id}/history`, {
          headers: { Authorization: `Bearer ${token}` }, cache: "no-store",
        }),
      ]);
      if (detailRef.current !== requestId) return;
      if (detailResponse.ok) setDetail(await detailResponse.json());
      if (historyResponse.ok) setHistory(await historyResponse.json());
      else setHistoryError("历史版本暂时无法读取");
    } catch {
      if (detailRef.current === requestId) setHistoryError("历史版本加载失败");
    }
  }

  async function saveEdit() {
    if (!detail || !editText.trim()) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`/api/memory/items/${detail.id}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ text: editText.trim() }),
      });
      if (!response.ok) throw new Error(`保存失败（${response.status}）`);
      const updated = await response.json() as MemoryItem;
      await openDetail(updated);
      await load(mode, query, offset, category);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function createItem() {
    if (!newText.trim()) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/memory/items", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          text: newText.trim(),
          kind: newKind,
          layer: newKind === "project_fact" ? "project" : "identity",
        }),
      });
      if (!response.ok) throw new Error(`新增失败（${response.status}）`);
      const nextCategory: Category = newKind === "project_fact" ? "project" : "stable";
      setCreating(false);
      setNewText("");
      setCategory(nextCategory);
      setMode("list");
      setQuery("");
      setDraft("");
      setOffset(0);
      await load("list", "", 0, nextCategory);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "新增失败");
    } finally {
      setBusy(false);
    }
  }

  async function removeItem() {
    if (!detail) return;
    if (!window.confirm("确认删除这条记忆及其历史版本？\n\n删除后不会再用于新对话，服务端仅保留审计记录。")) return;
    setBusy(true);
    try {
      const response = await fetch(`/api/memory/items/${detail.id}`, {
        method: "DELETE", headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error(`删除失败（${response.status}）`);
      setDetail(null);
      setHistory(null);
      await load(mode, query, offset, category);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "删除失败");
    } finally {
      setBusy(false);
    }
  }

  function chooseCategory(next: Category) {
    setCategory(next);
    setMode("list");
    setQuery("");
    setDraft("");
    setOffset(0);
    void load("list", "", 0, next);
  }

  function submitSearch() {
    const text = draft.trim();
    setOffset(0);
    setQuery(text);
    void load(text ? "search" : "list", text, 0, category);
  }

  function clearSearch() {
    setDraft("");
    setQuery("");
    setOffset(0);
    void load("list", "", 0, category);
  }

  return (
    <section className="memory-panel" aria-label="长期记忆">
      <header className="memory-head">
        <div>
          <h2>长期记忆</h2>
          <p className="memory-hint">保存会跨新对话使用的偏好、约束和项目事实。完整聊天记录请在“对话”中查看。</p>
        </div>
        <div className="memory-head-actions">
          <button type="button" onClick={() => setCreating((value) => !value)} disabled={busy}>
            <Plus size={15} />新增记忆
          </button>
          <button type="button" onClick={() => void load(mode, query, offset, category)} disabled={loading}>
            <RotateCcw size={15} />{loading ? "加载中…" : "刷新"}
          </button>
        </div>
      </header>

      <div className="memory-boundary">
        <span><strong>旧对话</strong>读取自身聊天记录</span>
        <span><strong>新对话</strong>只继承下方可用记忆</span>
        <span><strong>当前要求</strong>始终优先于旧记忆</span>
      </div>

      {creating && (
        <div className="memory-create">
          <label>
            类型
            <select value={newKind} onChange={(event) => setNewKind(event.target.value as typeof newKind)} aria-label="新增记忆类型">
              <option value="preference">个人偏好</option>
              <option value="constraint">行为约束</option>
              <option value="project_fact">项目事实</option>
            </select>
          </label>
          <textarea
            value={newText}
            onChange={(event) => setNewText(event.target.value)}
            rows={3}
            placeholder="例如：以后数据分析报告默认使用中文，并保留计算过程"
            aria-label="新增记忆内容"
          />
          <div className="memory-actions">
            <button type="button" onClick={() => void createItem()} disabled={busy || !newText.trim()}>保存记忆</button>
            <button type="button" onClick={() => { setCreating(false); setNewText(""); }} disabled={busy}>取消</button>
          </div>
        </div>
      )}

      <div className="memory-tabs" aria-label="记忆分类">
        {(Object.keys(CATEGORY_TEXT) as Category[]).map((value) => (
          <button
            type="button"
            key={value}
            className={category === value ? "selected" : ""}
            aria-pressed={category === value}
            onClick={() => chooseCategory(value)}
          >{CATEGORY_TEXT[value]}</button>
        ))}
      </div>

      <div className="memory-search">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && submitSearch()}
          placeholder="在当前分类中搜索"
          aria-label="搜索记忆"
        />
        <button type="button" onClick={submitSearch}>搜索</button>
        <button type="button" className="secondary" onClick={clearSearch}>清空搜索</button>
        <span className="memory-total">{mode === "search" ? `找到 ${total} 条（最多 50）` : `共 ${total} 条`}</span>
      </div>

      {error && <div className="memory-error">{error}<button type="button" onClick={() => void load(mode, query, offset, category)}>重试</button></div>}

      {!loading && items.length === 0 ? (
        <div className="memory-empty">
          <strong>{mode === "search" ? "没有找到相关记忆" : `${CATEGORY_TEXT[category]}暂无记录`}</strong>
          <p>可以在聊天中明确说“记住……”，也可以点击“新增记忆”手动添加。</p>
        </div>
      ) : (
        <div className="memory-table-wrap">
          <table className="memory-table">
            <thead><tr><th>内容</th><th>类型</th><th>用于新对话</th><th>来源</th><th>更新时间</th></tr></thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className={detail?.id === item.id ? "selected" : ""}>
                  <td><button type="button" className="memory-content" onClick={() => void openDetail(item)}>{item.text}</button></td>
                  <td><span className={`memory-kind ${item.kind}`}>{KIND_TEXT[item.kind]}</span></td>
                  <td><span className={`memory-usage ${item.context_usage}`}>{USAGE_TEXT[item.context_usage]}</span></td>
                  <td>{sourceText(item.source_ref)}</td>
                  <td className="memory-time">{formatTime(item.updated_at || item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {mode === "list" && total > 0 && (
        <div className="memory-pager">
          <button type="button" disabled={offset === 0} onClick={() => {
            const next = Math.max(0, offset - 20);
            setOffset(next);
            void load("list", "", next, category);
          }}>上一页</button>
          <span>{offset + 1}–{Math.min(offset + 20, total)} / {total}</span>
          <button type="button" disabled={offset + 20 >= total} onClick={() => {
            const next = offset + 20;
            setOffset(next);
            void load("list", "", next, category);
          }}>下一页</button>
        </div>
      )}

      {detail && (
        <aside className="memory-detail" aria-label="记忆详情">
          <header>
            <div><span className={`memory-kind ${detail.kind}`}>{KIND_TEXT[detail.kind]}</span><h3>记忆详情</h3></div>
            <div className="memory-actions">
              <button type="button" onClick={() => { setEditing(true); setEditText(detail.text); }} disabled={busy}>编辑</button>
              <button type="button" className="danger" onClick={() => void removeItem()} disabled={busy}>删除</button>
              <button type="button" aria-label="关闭详情" onClick={() => { setDetail(null); setHistory(null); }}><X size={15} /></button>
            </div>
          </header>

          {editing ? (
            <div className="memory-edit">
              <textarea value={editText} onChange={(event) => setEditText(event.target.value)} rows={4} aria-label="编辑记忆内容" />
              <p className="memory-hint">保存会生成新版本，旧内容仍保留在下方历史中。</p>
              <div className="memory-actions">
                <button type="button" onClick={() => void saveEdit()} disabled={busy || !editText.trim()}>保存新版本</button>
                <button type="button" onClick={() => setEditing(false)} disabled={busy}>取消</button>
              </div>
            </div>
          ) : <p className="memory-full">{detail.text}</p>}

          <dl className="memory-detail-grid">
            <div><dt>用于新对话</dt><dd>{USAGE_TEXT[detail.context_usage]}</dd></div>
            <div><dt>来源</dt><dd>{sourceText(detail.source_ref)}</dd></div>
            <div><dt>更新时间</dt><dd>{formatTime(detail.updated_at || detail.created_at)}</dd></div>
            <div><dt>当前状态</dt><dd>{detail.status === "active" ? "有效" : "已删除"}</dd></div>
          </dl>

          {detail.source_session_id && onOpenSession && (
            <button type="button" className="memory-source-link" onClick={() => onOpenSession(detail.source_session_id!)}>
              <ExternalLink size={15} />打开来源对话
            </button>
          )}

          <h4>历史版本（旧 → 新）</h4>
          {historyError && <p className="memory-error-text">{historyError}</p>}
          {!history && !historyError && <p className="memory-hint">正在读取历史版本…</p>}
          {history && (
            <>
              {history.truncated && <p className="memory-hint">历史较长，仅显示当前位置附近的版本。</p>}
              <ol className="memory-history">
                {history.items.map((row) => (
                  <li key={row.id} className={row.id === history.anchor_id ? "anchor" : ""}>
                    <div><strong>{STATUS_TEXT[row.version_status] ?? row.version_status}</strong><time>{formatTime(row.created_at)}</time></div>
                    <p>{row.text}</p>
                  </li>
                ))}
              </ol>
            </>
          )}
        </aside>
      )}
    </section>
  );
}
