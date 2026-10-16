import { rmSync } from 'node:fs'

// VuePress preserves outDir by default; remove pages deleted or moved since the last build.
rmSync(new URL('./dist/', import.meta.url), { recursive: true, force: true })
