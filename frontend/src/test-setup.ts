import "@testing-library/jest-dom/vitest";

/*
 * vitest's jsdom environment installs jsdom's AbortController / AbortSignal on the global
 * while `Request` stays Node's (undici), which brand-checks `init.signal` and throws
 * 'Expected signal to be an instance of AbortSignal' for a jsdom one. React Router's data
 * router builds a Request per navigation, so any test rendering the app's routes
 * (createMemoryRouter + useBlocker) would blow up. No route has a loader, so nothing
 * listens to that signal: drop it when it is the foreign kind.
 */
const NativeRequest = globalThis.Request;
class TestRequest extends NativeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    if (init?.signal && init.signal instanceof globalThis.AbortSignal) {
      const { signal: _foreign, ...rest } = init;
      super(input, rest);
      return;
    }
    super(input, init);
  }
}
globalThis.Request = TestRequest as typeof Request;
