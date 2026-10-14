/** Error carrying a stable `code` (e.g. `not_found`, `expired`,
 * `unsupported_schema`) so the UI can pick human copy without ever showing a
 * status code or URL. */
export class SessionExplorerError extends Error {
  code: string;
  constructor(message: string, code: string) {
    super(message);
    this.name = 'SessionExplorerError';
    this.code = code;
  }
}
