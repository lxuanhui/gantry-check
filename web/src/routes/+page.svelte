<script lang="ts">
	import { onMount } from 'svelte';
	import PlaceInput from '$lib/PlaceInput.svelte';
	import RouteMap from '$lib/RouteMap.svelte';
	import {
		explainError,
		getGantries,
		postEstimate,
		VEHICLES,
		type Estimate,
		type Gantry,
		type Vehicle
	} from '$lib/api';
	import type { Place } from '$lib/onemap';
	import {
		clockTime,
		describeDeparture,
		formatDistance,
		formatDuration,
		nowInSingapore
	} from '$lib/sgtime';

	let origin = $state<Place | null>(null);
	let destination = $state<Place | null>(null);
	let departAt = $state(nowInSingapore());
	let vehicle = $state<Vehicle>('car');

	let estimate = $state<Estimate | null>(null);
	let error = $state<{ title: string; detail: string } | null>(null);
	let loading = $state(false);
	let gantries = $state<Gantry[]>([]);

	let inflight: AbortController | undefined;

	onMount(() => {
		// Only used to place gantry markers on the map; a failure here is not worth surfacing.
		void getGantries()
			.then((list) => (gantries = list))
			.catch(() => (gantries = []));
	});

	function swap() {
		[origin, destination] = [destination, origin];
	}

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (!origin || !destination) {
			error = { title: 'Pick both ends of the trip', detail: 'Choose an origin and a destination from the suggestions.' };
			return;
		}

		inflight?.abort();
		const controller = new AbortController();
		inflight = controller;
		loading = true;
		error = null;

		try {
			const result = await postEstimate(
				{
					origin: [origin.lat, origin.lng],
					destination: [destination.lat, destination.lng],
					// Sent as-is: the API reads a naive timestamp as Singapore time.
					depart_at: departAt.length === 16 ? `${departAt}:00` : departAt,
					vehicle
				},
				controller.signal
			);
			if (controller !== inflight) return;
			estimate = result;
		} catch (thrown) {
			if (thrown instanceof DOMException && thrown.name === 'AbortError') return;
			if (controller !== inflight) return;
			estimate = null;
			error = explainError(thrown);
		} finally {
			if (controller === inflight) loading = false;
		}
	}
</script>

<form onsubmit={submit}>
	<PlaceInput id="origin" label="From" locate bind:place={origin} />

	<div class="swaprow">
		<button type="button" class="swap" onclick={swap} aria-label="Swap origin and destination">
			⇅ Swap
		</button>
	</div>

	<PlaceInput id="destination" label="To" bind:place={destination} />

	<div class="row">
		<div class="field">
			<div class="labelrow">
				<label for="depart">Departure (Singapore time)</label>
				<button type="button" class="ghost" onclick={() => (departAt = nowInSingapore())}>Now</button>
			</div>
			<input id="depart" type="datetime-local" bind:value={departAt} />
		</div>

		<div class="field">
			<label for="vehicle">Vehicle</label>
			<select id="vehicle" bind:value={vehicle}>
				{#each VEHICLES as option (option.value)}
					<option value={option.value}>{option.label}</option>
				{/each}
			</select>
		</div>
	</div>

	<button type="submit" class="primary" disabled={loading}>
		{loading ? 'Estimating…' : 'Estimate ERP'}
	</button>
</form>

{#if error}
	<section class="card error" aria-live="polite">
		<h2>{error.title}</h2>
		<p>{error.detail}</p>
	</section>
{/if}

{#if estimate}
	{@const result = estimate}
	<section class="card result" aria-live="polite">
		<p class="totallabel">Estimated ERP</p>
		<p class="total" class:free={result.total_cents === 0}>{result.total}</p>
		<p class="trip">
			{formatDistance(result.distance_m)} · {formatDuration(result.duration_s)} · leaving {describeDeparture(
				result.depart_at
			)}
		</p>
		<p class="meta">
			{result.summary || 'Route'} · {result.charges.length}
			{result.charges.length === 1 ? 'gantry' : 'gantries'} · routed by {result.engine}
		</p>

		{#if result.charges.length}
			<ul class="charges">
				{#each result.charges as charge (charge.gantry)}
					<li class:muted={charge.amount_cents === 0}>
						<span class="time">{clockTime(charge.crossed_at)}</span>
						<span class="who">
							<span class="gname"><b>{charge.gantry}</b> {charge.name}</span>
							<span class="detail">
								{#if charge.band}{charge.band.start}–{charge.band.end}{:else}no charge in this band{/if}
								{#if charge.method === 'point'}
									<span class="flag" title="Matched by proximity only — the carriageway direction could not be checked">
										direction unverified
									</span>
								{/if}
							</span>
						</span>
						<span class="amount">{charge.amount}</span>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="none">No ERP gantries on this route at that time.</p>
		{/if}

		{#if result.warnings.length}
			<ul class="warnings">
				{#each result.warnings as warning, index (index)}
					<li>{warning}</li>
				{/each}
			</ul>
		{/if}
	</section>
{/if}

<section class="card mapcard">
	<RouteMap {origin} {destination} {estimate} {gantries} />
</section>

<style>
	form {
		display: flex;
		flex-direction: column;
		gap: 0.85rem;
	}

	.swaprow {
		display: flex;
		justify-content: flex-end;
		margin: -0.55rem 0;
	}

	.swap {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.25rem 0.6rem;
		color: var(--muted);
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 999px;
		cursor: pointer;
	}

	.row {
		display: flex;
		flex-direction: column;
		gap: 0.85rem;
	}

	@media (min-width: 560px) {
		.row {
			flex-direction: row;
		}

		.row .field {
			flex: 1;
			min-width: 0;
		}
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.labelrow {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}

	label {
		font-size: 0.8rem;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--muted);
	}

	.ghost {
		background: none;
		border: 0;
		padding: 0;
		font: inherit;
		font-size: 0.8rem;
		color: var(--accent);
		cursor: pointer;
	}

	input,
	select {
		width: 100%;
		max-width: 100%;
		padding: 0.7rem 0.75rem;
		font: inherit;
		font-size: 1rem;
		color: var(--fg);
		background: var(--input-bg);
		border: 1px solid var(--border);
		border-radius: 10px;
	}

	.primary {
		margin-top: 0.25rem;
		padding: 0.85rem;
		font: inherit;
		font-size: 1rem;
		font-weight: 600;
		color: #fff;
		background: #1d4ed8;
		border: 0;
		border-radius: 10px;
		cursor: pointer;
	}

	.primary:disabled {
		opacity: 0.65;
		cursor: default;
	}

	.card {
		margin-top: 1.25rem;
		padding: 1rem;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 14px;
	}

	.mapcard {
		padding: 0.5rem;
	}

	.error h2 {
		margin: 0 0 0.3rem;
		font-size: 1rem;
		color: var(--danger);
	}

	.error p {
		margin: 0;
		font-size: 0.9rem;
		color: var(--muted);
	}

	.totallabel {
		margin: 0;
		font-size: 0.78rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--muted);
	}

	.total {
		margin: 0.1rem 0 0.3rem;
		font-size: 2.6rem;
		font-weight: 700;
		letter-spacing: -0.02em;
		line-height: 1.05;
	}

	.total.free {
		color: var(--good);
	}

	.trip {
		margin: 0;
		font-size: 0.95rem;
	}

	.meta {
		margin: 0.15rem 0 0;
		font-size: 0.78rem;
		color: var(--muted);
	}

	.charges {
		list-style: none;
		margin: 1rem 0 0;
		padding: 0;
		border-top: 1px solid var(--border);
	}

	.charges li {
		display: grid;
		grid-template-columns: auto 1fr auto;
		gap: 0.6rem;
		align-items: baseline;
		padding: 0.6rem 0;
		border-bottom: 1px solid var(--border);
	}

	.charges li.muted {
		color: var(--muted);
	}

	.time {
		font-variant-numeric: tabular-nums;
		font-size: 0.9rem;
		color: var(--muted);
	}

	.who {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		min-width: 0;
	}

	.gname {
		font-size: 0.92rem;
		overflow-wrap: anywhere;
	}

	.detail {
		font-size: 0.75rem;
		color: var(--muted);
	}

	.flag {
		display: inline-block;
		margin-left: 0.3rem;
		padding: 0 0.35rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		font-size: 0.7rem;
		color: var(--danger);
	}

	.amount {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
		white-space: nowrap;
	}

	.none {
		margin: 0.9rem 0 0;
		font-size: 0.9rem;
		color: var(--muted);
	}

	.warnings {
		margin: 0.9rem 0 0;
		padding-left: 1.1rem;
		font-size: 0.78rem;
		color: var(--muted);
	}
</style>
