<svelte:head>
	<title>Terms — ERP check</title>
	<meta name="description" content="Terms of use, data sources and attribution for ERP check." />
</svelte:head>

<section class="card">
	<h2>Terms</h2>
	<p class="updated">Last updated: 21 September 2026</p>

	<h3>Estimates only — no warranty, no liability</h3>
	<p>
		This site gives you an estimate, not an official figure. Crossing times are modelled from the
		routed path, route matching against gantries can be wrong, and rates are refreshed from LTA
		weekly so they may briefly lag a real change. Don't rely on this for disputes or payments —
		the authoritative source is LTA / OneMotoring. The site is provided free, as-is, with no
		warranty of any kind, and we're not liable for decisions made from it.
	</p>

	<h3>Acceptable use</h3>
	<p>
		This is a free tool for personal use — checking what a trip might cost before you drive it.
		Please don't hammer the API with automated or bulk requests. Estimates are rate limited to 20
		per minute per IP address, and the service only answers requests that Cloudflare attributes to
		Singapore.
	</p>

	<h3>Data sources and attribution</h3>
	<ul>
		<li>
			ERP rates from
			<a href="https://datamall.lta.gov.sg/" target="_blank" rel="noreferrer">LTA DataMall</a>'s
			per-gantry rate tables and the ERP Rates PDF, used under the LTA DataMall Terms of Use.
		</li>
		<li>Gantry coordinates from LTA's OneMotoring gantry KML.</li>
		<li>
			Gantry lines ("LTA Gantry (GEOJSON)") and public holidays from
			<a href="https://data.gov.sg/" target="_blank" rel="noreferrer">data.gov.sg</a>, made
			available under the
			<a href="https://data.gov.sg/open-data-licence" target="_blank" rel="noreferrer"
				>Singapore Open Data Licence version 1.0</a
			>. Contains information from LTA Gantry (GEOJSON) and Public Holidays accessed from
			data.gov.sg which is made available under the terms of the Singapore Open Data Licence
			version 1.0.
		</li>
		<li>
			Map data © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer"
				>OpenStreetMap</a
			> contributors.
		</li>
		<li>
			Address search by <a href="https://www.onemap.gov.sg/" target="_blank" rel="noreferrer"
				>OneMap</a
			> (© Singapore Land Authority).
		</li>
		<li>Routing by Google.</li>
	</ul>

	<h3>Third-party terms</h3>
	<p>
		Parts of this site depend on other services, and their own terms apply to those parts: the
		<a href="https://cloud.google.com/maps-platform/terms" target="_blank" rel="noreferrer"
			>Google Maps Platform Terms of Service</a
		>
		for routing, the
		<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer"
			>OpenStreetMap</a
		>
		copyright and licence for map tiles, OneMap's terms for address search, and the
		<a href="https://data.gov.sg/open-data-licence" target="_blank" rel="noreferrer"
			>Singapore Open Data Licence v1.0</a
		> for data.gov.sg datasets.
	</p>

	<h3>Contact</h3>
	<p>
		Questions about these terms? Open an issue at
		<a href="https://github.com/lxuanhui/gantry-check/issues" target="_blank" rel="noreferrer"
			>github.com/lxuanhui/gantry-check/issues</a
		>.
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
