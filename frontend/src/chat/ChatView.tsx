// 聊天视图（从 App.tsx 拆出，W4）
//
// 只负责渲染：消息列表、工具审批卡、生成中提示、输入框。
// 状态与请求仍留在 App 里 —— 拆视图是为了后续加产物卡片与摘要卡，
// 不是为了顺手重写聊天逻辑。

import type {
  FormEvent,
  KeyboardEvent,
  RefObject,
} from "react";

import {
  ArrowUp,
  ArrowUpRight,
  BellRing,
  CalendarDays,
  CloudDownload,
  Lightbulb,
  Mail,
  RefreshCw,
  Square,
} from "lucide-react";

import ArtifactCard from "../components/ArtifactCard";
import ActivitySummary from "../components/ActivitySummary";
import BrandMark from "../components/BrandMark";
import type {
  ActivitySummaryPayload,
  ArtifactMeta,
  Message,
} from "./types";

type PendingApproval = {
  tool: string;
  preview: string;
};

type ChatMessagesProps = {
  messages: Message[];
  pendingApproval: PendingApproval | null;
  decideApproval: (decision: "approve" | "reject") => void;
  sending: boolean;
  activeTool: string | null;
  finishedTool: string | null;
  checkingId: string | null;
  queryStatus: (requestId: string) => void;
  recoveryNotice: { requestId: string; message: string } | null;
  bottomRef: RefObject<HTMLDivElement | null>;
  artifacts: ArtifactMeta[];
  activeArtifactId: string | null;
  onOpenArtifact: (artifactId: string) => void;
  onSuggestion: (prompt: string) => void;
  activity: ActivitySummaryPayload | null;
};

const STARTERS = [
  {
    title: "规划今天",
    description: "把待办整理成清晰的行动计划",
    prompt: "帮我规划今天最重要的三件事，并给出可执行的时间安排。",
    icon: <CalendarDays size={19} />,
  },
  {
    title: "定时通知",
    description: "让重要的小事按时找到你",
    prompt: "10 分钟后提醒我起身活动一下。",
    icon: <BellRing size={19} />,
  },
  {
    title: "解释概念",
    description: "把复杂问题讲得简单准确",
    prompt: "用一个生活里的例子，帮我解释一个我正在学习的复杂概念。",
    icon: <Lightbulb size={19} />,
  },
  {
    title: "起草邮件",
    description: "从要点开始写一封自然的邮件",
    prompt: "帮我起草一封专业但自然的邮件，先向我询问必要的信息。",
    icon: <Mail size={19} />,
  },
];

export function ChatMessages({
  messages,
  pendingApproval,
  decideApproval,
  sending,
  activeTool,
  finishedTool,
  checkingId,
  queryStatus,
  recoveryNotice,
  bottomRef,
  artifacts,
  activeArtifactId,
  onOpenArtifact,
  onSuggestion,
  activity,
}: ChatMessagesProps) {
  return (
    <>
      <section className="chat-area">
        {messages.length === 0 && (
          <div className="welcome">
            <div className="welcome-heading">
              <div className="welcome-logo"><BrandMark size={34} /></div>
              <span className="welcome-kicker">你的个人工作空间</span>
              <h2>把想法，<br />推进成结果。</h2>
              <p>对话、记忆和工具在同一条工作流里。<br />从一个任务开始，剩下的交给你的助手。</p>
            </div>

            <div className="starter-grid" aria-label="快捷指令">
              {STARTERS.map((starter) => (
                <button
                  type="button"
                  className="starter-card"
                  key={starter.title}
                  onClick={() => onSuggestion(starter.prompt)}
                >
                  <span className="starter-icon">{starter.icon}</span>
                  <span className="starter-copy">
                    <strong>{starter.title}</strong>
                    <small>{starter.description}</small>
                  </span>
                  <ArrowUpRight className="starter-arrow" size={17} />
                </button>
              ))}
            </div>

            <div className="welcome-tip">本地数据 · 工具留痕 · 结果可核对</div>
          </div>
        )}

        <ActivitySummary summary={activity} />

        {artifacts.length > 0 && (
          <section
            className="artifact-strip"
            aria-label="本次产物"
            data-testid="artifact-strip"
          >
            <header className="artifact-strip-head">
              <span>本次产物 · {artifacts.length}</span>
            </header>

            <div className="artifact-cards">
              {artifacts.map((artifact) => (
                <ArtifactCard
                  key={artifact.id}
                  artifact={artifact}
                  active={artifact.id === activeArtifactId}
                  onOpen={onOpenArtifact}
                />
              ))}
            </div>
          </section>
        )}

        <div className="message-container">
          {messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <div className="message-avatar">
                {message.role === "assistant" ? <BrandMark size={20} /> : "你"}
              </div>

              <div className="message-body">
                <div className="message-name">
                  {message.role === "assistant" ? "Personal Agent" : "你"}
                </div>

                <div className="message-content">{message.content}</div>

                {message.status === "pending" &&
                  !(message.role === "user" && message.requestId) && (
                    <div className="message-status">等待回复…</div>
                  )}

                {message.status === "unknown" &&
                  !(message.role === "user" && message.requestId) && (
                    <div className="message-status warning">请求结果尚未确认</div>
                  )}

                {message.status === "rejected" && (
                  <div className="message-status warning">
                    未发送成功 · AI 未执行
                  </div>
                )}

                {message.status === "cancelled" && (
                  <div className="message-status">已取消本次回复</div>
                )}

                {message.role === "user" &&
                  message.requestId &&
                  (message.status === "pending" ||
                    message.status === "unknown") && (
                    <div
                      className={`reply-recovery${message.status === "unknown" ? " is-unknown" : ""}`}
                      data-testid="reply-recovery"
                      role={message.status === "unknown" || recoveryNotice?.requestId === message.requestId ? "alert" : "group"}
                      aria-label="回复恢复状态"
                    >
                      <span className="reply-recovery-icon" aria-hidden="true">
                        <CloudDownload size={18} />
                      </span>

                      <div className="reply-recovery-copy" aria-live="polite">
                        <strong className={`message-status${message.status === "unknown" ? " warning" : ""}`}>
                          {checkingId === message.requestId
                            ? "正在获取回复"
                            : message.status === "unknown"
                              ? "请求结果尚未确认"
                              : "等待 AI 回复"}
                        </strong>
                        <p>
                          {checkingId === message.requestId
                            ? "正在向服务器确认处理结果，请稍候。"
                            : recoveryNotice?.requestId === message.requestId
                              ? recoveryNotice.message
                              : message.status === "unknown"
                                ? "连接可能中断。先获取这条消息的回复，避免重复发送。"
                                : "消息已发送。如果等待过久或页面中断，可手动获取结果。"}
                        </p>
                      </div>

                      <button
                        type="button"
                        className="reply-recovery-action"
                        onClick={() => void queryStatus(message.requestId!)}
                        disabled={checkingId === message.requestId}
                        aria-label="获取上一条 AI 回复"
                      >
                        <RefreshCw
                          size={15}
                          className={checkingId === message.requestId ? "is-spinning" : undefined}
                          aria-hidden="true"
                        />
                        <span>{checkingId === message.requestId ? "获取中…" : "获取回复"}</span>
                      </button>
                    </div>
                  )}
              </div>
            </article>
          ))}

          {pendingApproval && (
            <div
              className="approval-card"
              role="alertdialog"
              aria-label="工具审批"
            >
              <div className="approval-title">
                工具「{pendingApproval.tool}」需要你的批准
              </div>
              <pre className="approval-preview">{pendingApproval.preview}</pre>
              <div className="approval-actions">
                <button type="button" onClick={() => decideApproval("approve")}>
                  批准执行
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => decideApproval("reject")}
                >
                  拒绝
                </button>
              </div>
            </div>
          )}

          {sending && (
            <div className="loading" role="status">
              <div className="spinner" />

              <span>
                {pendingApproval
                  ? "等待你批准工具调用…"
                  : activeTool
                    ? `正在执行工具：${activeTool}（Esc 停止）`
                    : finishedTool
                      ? `工具 ${finishedTool} 已完成，正在生成回答…`
                      : "助手正在生成，按 Esc 停止…"}
              </span>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </section>
    </>
  );
}

type ComposerProps = {
  error: string;
  input: string;
  setInput: (value: string) => void;
  sending: boolean;
  readOnly?: boolean;
  handleKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  sendMessage: (event: FormEvent<HTMLFormElement>) => void;
  cancelActiveRun: () => void;
};

export function Composer({
  error,
  input,
  setInput,
  sending,
  readOnly = false,
  handleKeyDown,
  sendMessage,
  cancelActiveRun,
}: ComposerProps) {
  return (
    <div className="input-section">
      {readOnly && (
        <div className="session-readonly-note" role="status">
          正在查看 QQ 对话记录。请回到 QQ 继续聊天，或点击“新对话”。
        </div>
      )}
      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}

      <form className="input-box" onSubmit={sendMessage}>
        <textarea
          aria-label="输入聊天消息"
          placeholder={readOnly ? "QQ 历史记录仅查看" : "写下你想完成的事，或输入一个想法…"}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={sending || readOnly}
          maxLength={8000}
          rows={2}
        />

        <div className="input-actions">
          <span className="keyboard-hint"><kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行</span>
          <span className="character-count">{input.length > 0 ? `${input.length} / 8000` : ""}</span>

          {sending ? (
            <button type="button" className="stop-button" onClick={cancelActiveRun} aria-label="停止生成">
              <Square size={13} fill="currentColor" /><span>停止</span><kbd>Esc</kbd>
            </button>
          ) : (
            <button type="submit" className="send-button" disabled={readOnly || !input.trim()} aria-label="发送 ↑">
              <span>发送</span><ArrowUp size={17} />
            </button>
          )}
        </div>
      </form>

      <p className="footer-note">
        本地工作空间 · 聊天记录保存在本地并按会话隔离
      </p>
    </div>
  );
}
