<script lang="ts">
	import { page } from '$app/state';

	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();

	// The Worker answers on both its workers.dev address and this custom domain, which would
	// otherwise look like duplicate sites to a crawler. The canonical URL names the one that
	// counts, and it is also the origin the Google Maps browser key is restricted to.
	const SITE = 'https://erp.xuanhuilee.com';
	const canonical = $derived(SITE + page.url.pathname);
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<link rel="canonical" href={canonical} />
	<title>ERP check — Singapore gantry fare estimate</title>
	<meta
		name="description"
		content="Estimate the ERP charge on a Singapore drive: which gantries a route crosses, when, and what each one costs."
	/>
	<meta name="color-scheme" content="light dark" />
	<!-- Tints the phone's browser chrome to match the page instead of leaving a white or
	     black band above it. -->
	<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f6f7f9" />
	<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0f1216" />
</svelte:head>

<div class="shell">
	<header>
		<h1>ERP check</h1>
		<p>What should the ERP on this trip cost?</p>
	</header>

	{@render children()}

	<footer>
		<p class="sources">
			Rates from LTA's per-gantry rate tables; gantry locations from the OneMotoring gantry KML
			and data.gov.sg's LTA Gantry lines; address search by
			<a href="https://www.onemap.gov.sg/" target="_blank" rel="noreferrer">OneMap</a>.
		</p>
		<p>
			<a href="/docs" data-sveltekit-reload>API docs</a> · <a href="/privacy">Privacy</a> ·
			<a href="/terms">Terms</a> · Estimates only — crossing times are modelled from the route,
			not measured.
		</p>
	</footer>
</div>

<style>
	:global(:root) {
		--bg: #f6f7f9;
		--card: #ffffff;
		--input-bg: #ffffff;
		--fg: #15181d;
		--muted: #646b76;
		--border: #d9dde3;
		--hover: #eef1f5;
		--accent: #1d4ed8;
		--danger: #b91c1c;
		--good: #166534;
	}

	@media (prefers-color-scheme: dark) {
		:global(:root) {
			--bg: #0f1216;
			--card: #171b21;
			--input-bg: #11151a;
			--fg: #e8eaee;
			--muted: #9aa2ad;
			--border: #2b323b;
			--hover: #1f252c;
			--accent: #7aa2ff;
			--danger: #f87171;
			--good: #4ade80;
		}
	}

	:global(*) {
		box-sizing: border-box;
	}

	:global(html) {
		background: var(--bg);
	}

	:global(body) {
		margin: 0;
		background: var(--bg);
		color: var(--fg);
		font-family:
			system-ui,
			-apple-system,
			'Segoe UI',
			Roboto,
			sans-serif;
		line-height: 1.45;
		-webkit-text-size-adjust: 100%;
	}

	/* Phone first: the 16px gutter is the floor, widened only where a notch or a rounded
	   corner eats into the edge (viewport-fit=cover in app.html opts the page into that
	   area). The bottom inset keeps the footer clear of the home indicator. */
	.shell {
		max-width: 44rem;
		margin: 0 auto;
		padding: calc(1rem + env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
			calc(2.5rem + env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
	}

	header h1 {
		margin: 0;
		font-size: 1.35rem;
		letter-spacing: -0.01em;
	}

	header p {
		margin: 0.1rem 0 1rem;
		color: var(--muted);
		font-size: 0.88rem;
	}

	/* Above phone width the header can afford its original size and breathing room. */
	@media (min-width: 560px) {
		.shell {
			padding-top: calc(1.25rem + env(safe-area-inset-top));
		}

		header h1 {
			font-size: 1.5rem;
		}

		header p {
			margin: 0.15rem 0 1.25rem;
			font-size: 0.92rem;
		}
	}

	footer {
		margin-top: 2rem;
		padding-top: 1rem;
		border-top: 1px solid var(--border);
		font-size: 0.76rem;
		color: var(--muted);
	}

	footer p {
		margin: 0 0 0.5rem;
	}

	footer a {
		color: var(--accent);
	}
</style>
