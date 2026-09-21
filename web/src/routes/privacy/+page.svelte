<svelte:head>
	<title>Privacy — ERP check</title>
	<meta name="description" content="What ERP check collects, what third parties see, and how to get in touch." />
</svelte:head>

<section class="card">
	<h2>Privacy</h2>
	<p class="updated">Last updated: 21 September 2026</p>

	<h3>What this site collects</h3>
	<p>
		Nothing. We don't store your trips, your location, or anything else about a request in our
		database — it only holds public rate and gantry data. But an estimate still touches a few
		other services along the way, and each one sees part of the request:
	</p>
	<ul>
		<li>
			<b>OneMap.</b> When you type an address, the search box sends that text straight from your
			browser to <a href="https://www.onemap.gov.sg/" target="_blank" rel="noreferrer">OneMap</a>,
			a Singapore government service — it never passes through our server.
		</li>
		<li>
			<b>Google Routes.</b> To estimate a trip, your browser sends the origin, destination and
			departure time to our server, which forwards the coordinates and time to the Google Routes
			API to compute the route. Google sees that request; we don't send your address text, only
			coordinates.
		</li>
		<li>
			<b>Google Maps.</b> The result map is the Google Maps JavaScript API, loaded and drawn
			directly by your browser from Google's servers, so Google sees your IP address and which
			map area you're viewing. Google's
			<a href="https://policies.google.com/privacy" target="_blank" rel="noreferrer"
				>privacy policy</a
			> applies to that.
		</li>
		<li>
			<b>Cloudflare.</b> The site is hosted on Cloudflare Workers, so Cloudflare sees your IP
			address and country on every request. We use the country to restrict estimates to
			Singapore, and a short-lived per-IP counter (about a minute) to rate-limit estimates to 20
			per minute. Cloudflare's request logging is on, so it keeps basic request logs — method,
			path, status, timing, not request bodies — for a short period under Cloudflare's own
			retention.
		</li>
	</ul>

	<h3>Cookies and tracking</h3>
	<p>
		We set none: no cookies, no accounts, no analytics, no localStorage, no ads, no tracking
		scripts. The embedded Google map is loaded from Google, and Google may set its own cookies
		when you use it; Google's privacy policy covers those.
	</p>

	<h3>Your choices and contact</h3>
	<p>
		Because we don't store anything about your requests, there's nothing on our side to access,
		export or delete. If you have a privacy or security question, open an issue on
		<a href="https://github.com/lxuanhui/gantry-check/issues" target="_blank" rel="noreferrer"
			>GitHub</a
		>.
	</p>

	<h3>Changes</h3>
	<p>
		If this changes — for example, if we ever add a new third-party service — we'll update this
		page and the "last updated" date above.
	</p>

	<a class="back" href="/">← Back to the estimator</a>
</section>

<style>
	.card {
		padding: 1rem;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 14px;
	}

	h2 {
		margin: 0 0 0.2rem;
		font-size: 1.3rem;
		letter-spacing: -0.01em;
	}

	.updated {
		margin: 0 0 1.25rem;
		font-size: 0.78rem;
		color: var(--muted);
	}

	h3 {
		margin: 1.25rem 0 0.4rem;
		font-size: 0.95rem;
	}

	p,
	li {
		margin: 0 0 0.6rem;
		font-size: 0.92rem;
		line-height: 1.55;
	}

	ul {
		margin: 0 0 0.6rem;
		padding-left: 1.2rem;
	}

	a {
		color: var(--accent);
	}

	.back {
		display: inline-block;
		margin-top: 1.25rem;
		font-size: 0.9rem;
	}
</style>
