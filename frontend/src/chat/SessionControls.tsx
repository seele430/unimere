import { useEffect, useState } from "react";
import { MessageSquareText, Plus } from "lucide-react";

type SessionSummary = {
  id: string;
  source: "web" | "qq";
  title: string;
  message_count: number;
  last_message_at: number | null;
};

type SessionListResponse = {
  sessions?: SessionSummary[];
  web_sessions?: SessionSummary[];
  qq_sessions?: SessionSummary[];
};

function SessionOptions({ sessions }: { sessions: SessionSummary[] }) {
  return sessions.map((item) => (
    <option key={item.id} value={item.id}>
      {item.title || "未命名对话"}（{item.message_count}）
    </option>
  ));
}

export default function SessionControls({
  authToken,
  sessionId,
  disabled,
  refreshToken,
  onSelect,
  onNew,
}: {
  authToken?: string;
  sessionId: string;
  disabled: boolean;
  refreshToken: number;
  onSelect: (sessionId: string) => void;
  onNew: () => void;
}) {
  const [webSessions, setWebSessions] = useState<SessionSummary[]>([]);
  const [qqSessions, setQqSessions] = useState<SessionSummary[]>([]);

  useEffect(() => {
    if (!authToken) return;
    let cancelled = false;

    fetch("/api/sessions", {
      headers: { Authorization: `Bearer ${authToken}` },
      cache: "no-store",
    })
      .then(async (response) => {
        if (!response.ok) return;
        const data = await response.json() as SessionListResponse;
        if (!cancelled) {
          const web = data.web_sessions ?? data.sessions ?? [];
          setWebSessions(Array.isArray(web) ? web : []);
          setQqSessions(Array.isArray(data.qq_sessions) ? data.qq_sessions : []);
        }
      })
      .catch(() => {
        // 会话列表失败不影响当前聊天；服务端消息仍然保留。
      });

    return () => {
      cancelled = true;
    };
  }, [authToken, refreshToken]);

  const sessions = [...webSessions, ...qqSessions];

  return (
    <div className="session-controls">
      <MessageSquareText size={16} aria-hidden="true" />
      <select
        aria-label="历史对话"
        title="网页对话可续聊；QQ 对话仅查看记录"
        value={sessionId}
        disabled={disabled || !authToken}
        onChange={(event) => event.target.value && onSelect(event.target.value)}
      >
        {!sessionId && <option value="">当前新对话</option>}
        {sessionId && !sessions.some((item) => item.id === sessionId) && (
          <option value={sessionId}>当前对话</option>
        )}
        <optgroup label="网页对话">
          <SessionOptions sessions={webSessions} />
        </optgroup>
        {qqSessions.length > 0 && (
          <optgroup label="QQ 对话（只读）">
            <SessionOptions sessions={qqSessions} />
          </optgroup>
        )}
      </select>
      <button
        type="button"
        className="tool-action new-chat-toggle"
        onClick={onNew}
        disabled={disabled}
        title="新建网页对话，不携带旧聊天记录"
      >
        <Plus size={16} /><span>新对话</span>
      </button>
    </div>
  );
}
