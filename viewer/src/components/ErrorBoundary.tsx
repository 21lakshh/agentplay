import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * Catches a render-time throw in `children` and shows `fallback` instead of
 * unmounting the whole app. Give it a `key` that changes with the thing it
 * guards (e.g. the session id) so moving on resets it.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; fallback: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('render failed', error, info.componentStack);
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
