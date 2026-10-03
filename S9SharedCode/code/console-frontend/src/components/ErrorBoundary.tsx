import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props { children: ReactNode }
interface State { err: Error | null }

/** Last line of defence for the render tree.
    Without this, one bad value — a node result shaped differently than the
    type claims, a null where an object was expected — throws inside React
    and unmounts EVERYTHING: the user gets a blank page and loses the run
    they were inspecting, with no error text and no way back except a
    reload. That is exactly what happened when a planner's `successors`
    (objects) were rendered as if they were strings. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { err: null };

  static getDerivedStateFromError(err: Error): State { return { err }; }

  componentDidCatch(err: Error, info: ErrorInfo): void {
    console.error('[aria] render error', err, info.componentStack);
  }

  private reset = () => this.setState({ err: null });

  render(): ReactNode {
    const { err } = this.state;
    if (!err) return this.props.children;
    return (
      <div className="flex h-full items-center justify-center bg-[#090909] p-6">
        <div className="w-full max-w-[520px] rounded-xl border border-red-400/30 bg-red-400/5 p-5">
          <div className="mb-1 text-xs font-bold tracking-[0.12em] text-red-300">SOMETHING BROKE RENDERING</div>
          <p className="mb-3 text-[13px] leading-relaxed text-zinc-300">
            This view hit an unexpected value. The rest of the console is
            unaffected — retry, or head to another section.
          </p>
          <pre className="mb-3 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-white/10 bg-black/40 p-2.5 font-mono text-[11.5px] text-red-200">
            {String(err && err.message ? err.message : err)}
          </pre>
          <div className="flex gap-2">
            <button
              onClick={this.reset}
              className="rounded-md bg-violet-400 px-3 py-1.5 text-xs font-bold text-[#0b0b0e] hover:brightness-110"
            >
              Try again
            </button>
            <button
              onClick={() => { window.location.href = '/console'; }}
              className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs font-bold text-zinc-200 hover:border-violet-400"
            >
              Go to Chat
            </button>
          </div>
        </div>
      </div>
    );
  }
}
