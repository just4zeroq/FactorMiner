/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Empty string means "same origin" (nginx / vite dev proxy). */
  readonly VITE_API_BASE?: string;
  /** Build-time token for local/trusted deployments; inlined into the bundle. */
  readonly VITE_API_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
