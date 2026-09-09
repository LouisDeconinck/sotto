/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ORGANISATION_DELETION_ENABLED?: string;
  /** This deployment's own status page. Unset means no status link is shown. */
  readonly VITE_STATUS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module "*.wasm?url" {
  const src: string;
  export default src;
}
