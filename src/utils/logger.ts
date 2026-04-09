/**
 * Simple logging utility.
 */

export function createLogger(name: string) {
  return {
    info: (...args: unknown[]) => console.log(`[${name}]`, ...args),
    warn: (...args: unknown[]) => console.warn(`[${name}]`, ...args),
    error: (...args: unknown[]) => console.error(`[${name}]`, ...args),
    debug: (...args: unknown[]) => {
      if (process.env.DEBUG) console.debug(`[${name}]`, ...args);
    },
  };
}
