import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// Every API path the UI (and FastAPI's own docs) uses, proxied to a locally running
// `uvicorn`/`pywrangler dev` API. In production the same paths are served by the Python
// Worker that also serves this build output as static assets, so the UI always uses
// relative URLs.
const apiPaths = ['/estimate', '/gantries', '/rates', '/health', '/docs', '/openapi.json'];
const proxy = Object.fromEntries(
	apiPaths.map((path) => [path, { target: 'http://127.0.0.1:8787', changeOrigin: true }])
);

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// Fully prerendered static output, uploaded as Cloudflare Workers static assets
			// (see `assets.directory` in ../wrangler.jsonc).
			adapter: adapter(),

			prerender: {
				// The footer links to FastAPI's /docs, which the Python Worker serves at runtime
				// (unmatched asset paths fall through to it). There is nothing for the crawler
				// to prerender there, so its 404 is expected.
				handleHttpError: ({ path, message }) => {
					if (apiPaths.includes(path)) return;
					throw new Error(message);
				}
			}
		})
	],
	server: { proxy }
});
