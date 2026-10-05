`anthropic-sdk.mjs` is the official Anthropic TypeScript SDK (`@anthropic-ai/sdk` 0.131.0)
bundled for the browser, so the app needs no CDN or build step:

    npm install @anthropic-ai/sdk esbuild
    echo 'export { default } from "@anthropic-ai/sdk"; export * from "@anthropic-ai/sdk";' > entry.js
    npx esbuild entry.js --bundle --format=esm --platform=browser --minify --outfile=anthropic-sdk.mjs
