import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  name: string;
  children: ReactNode;
}

interface State {
  err: string | null;
}

export class PaneErrorBoundary extends Component<Props, State> {
  state: State = { err: null };

  static getDerivedStateFromError(error: Error): State {
    return { err: error.message || String(error) };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[pane ${this.props.name}]`, error, info.componentStack);
  }

  render() {
    if (this.state.err) {
      return (
        <div className="pane-crash">
          <strong>{this.props.name} 面板出错</strong>
          <pre>{this.state.err}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}
