/** Minimal ambient types for the cache test.
 *
 *  The suite runs under vitest via npx; vitest is not an app dependency and
 *  adding it would ship a test runner to users. Declaring the few symbols used
 *  keeps `tsc --noEmit` honest about the test without pulling the package in.
 */
declare module 'vitest' {
  export const describe: (name: string, fn: () => void) => void;
  export const it: (name: string, fn: () => void | Promise<void>) => void;
  export const expect: (actual: unknown) => any;
  export const beforeEach: (fn: () => void) => void;
  export const vi: {
    fn: (impl?: (...a: any[]) => any) => any;
    useRealTimers: () => void;
    useFakeTimers: () => void;
    advanceTimersByTime: (ms: number) => void;
  };
}

/** The two tests that read source files as text need `node:fs` and
 *  `node:path`. `@types/node` is not a dependency of this app - it would ship
 *  server-side types to a browser bundle - so the two used entry points are
 *  declared here instead. */
declare module 'node:fs' {
  export const readFileSync: (p: string, enc: string) => string;
}
declare module 'node:path' {
  export const resolve: (...parts: string[]) => string;
}
declare const __dirname: string;