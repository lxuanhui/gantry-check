<script lang="ts">
	import { searchPlaces, type Place } from '$lib/onemap';

	interface Props {
		id: string;
		label: string;
		placeholder?: string;
		/** Show a "use my location" button (origin only). */
		locate?: boolean;
		place?: Place | null;
	}

	let { id, label, placeholder = 'Search an address or place', locate = false, place = $bindable(null) }: Props =
		$props();

	let query = $state(place?.name ?? '');
	let results = $state<Place[]>([]);
	let open = $state(false);
	let busy = $state(false);
	let highlighted = $state(-1);
	let message = $state('');

	// Keep the text box in step when the parent replaces the place (swap, geolocation).
	let seen: Place | null = place;
	$effect(() => {
		if (place !== seen) {
			seen = place;
			if (place) query = place.name;
		}
	});

	let debounce: ReturnType<typeof setTimeout> | undefined;
	let controller: AbortController | undefined;
	// Monotonic request id: a slow response that is no longer the latest is dropped.
	let issued = 0;

	function schedule(text: string) {
		clearTimeout(debounce);
		controller?.abort();
		const trimmed = text.trim();
		if (trimmed.length < 2) {
			results = [];
			open = false;
			busy = false;
			return;
		}
		busy = true;
		debounce = setTimeout(() => void run(trimmed), 300);
	}

	async function run(text: string) {
		const id = ++issued;
		controller = new AbortController();
		try {
			const found = await searchPlaces(text, controller.signal);
			if (id !== issued) return;
			results = found;
			highlighted = -1;
			open = true;
			message = found.length ? '' : 'No matches in OneMap.';
		} catch (error) {
			if (id !== issued || (error instanceof DOMException && error.name === 'AbortError')) return;
			results = [];
			open = true;
			message = 'Address search is unavailable right now.';
		} finally {
			if (id === issued) busy = false;
		}
	}

	function onInput(event: Event) {
		query = (event.target as HTMLInputElement).value;
		place = null;
		seen = null;
		message = '';
		schedule(query);
	}

	function choose(chosen: Place) {
		place = chosen;
		seen = chosen;
		query = chosen.name;
		results = [];
		open = false;
		message = '';
		issued++; // discard anything still in flight
	}

	function onKeydown(event: KeyboardEvent) {
		if (!open || !results.length) return;
		if (event.key === 'ArrowDown') {
			event.preventDefault();
			highlighted = (highlighted + 1) % results.length;
		} else if (event.key === 'ArrowUp') {
			event.preventDefault();
			highlighted = (highlighted - 1 + results.length) % results.length;
		} else if (event.key === 'Enter' && highlighted >= 0) {
			event.preventDefault();
			choose(results[highlighted]);
		} else if (event.key === 'Escape') {
			open = false;
		}
	}

	let locating = $state(false);

	function useMyLocation() {
		if (!navigator.geolocation) {
			message = 'This browser has no location support.';
			return;
		}
		locating = true;
		message = '';
		navigator.geolocation.getCurrentPosition(
			(position) => {
				locating = false;
				choose({
					name: 'My location',
					address: `${position.coords.latitude.toFixed(5)}, ${position.coords.longitude.toFixed(5)}`,
					lat: position.coords.latitude,
					lng: position.coords.longitude
				});
			},
			() => {
				locating = false;
				message = 'Could not get your location.';
			},
			{ enableHighAccuracy: true, timeout: 10000 }
		);
	}
</script>

<div class="field">
	<div class="labelrow">
		<label for={id}>{label}</label>
		{#if locate}
			<button type="button" class="ghost" onclick={useMyLocation} disabled={locating}>
				{locating ? 'Locating…' : 'Use my location'}
			</button>
		{/if}
	</div>

	<div class="combo">
		<input
			{id}
			type="text"
			autocomplete="off"
			spellcheck="false"
			role="combobox"
			aria-expanded={open}
			aria-controls="{id}-list"
			{placeholder}
			value={query}
			oninput={onInput}
			onkeydown={onKeydown}
			onfocus={() => {
				if (results.length) open = true;
			}}
			onblur={() => setTimeout(() => (open = false), 150)}
		/>
		{#if busy}<span class="spinner" aria-hidden="true"></span>{/if}

		{#if open && (results.length || message)}
			<ul class="results" id="{id}-list" role="listbox">
				{#each results as result, index (index)}
					<li>
						<button
							type="button"
							role="option"
							aria-selected={index === highlighted}
							class:highlighted={index === highlighted}
							onmousedown={(event) => event.preventDefault()}
							onclick={() => choose(result)}
						>
							<span class="rname">{result.name}</span>
							{#if result.address}<span class="raddr">{result.address}</span>{/if}
						</button>
					</li>
				{:else}
					<li class="empty">{message}</li>
				{/each}
			</ul>
		{/if}
	</div>

	{#if place}
		<p class="chosen">{place.address || `${place.lat.toFixed(5)}, ${place.lng.toFixed(5)}`}</p>
	{:else if message && !open}
		<p class="chosen warn">{message}</p>
	{/if}
</div>

<style>
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

	.ghost:disabled {
		color: var(--muted);
		cursor: default;
	}

	.combo {
		position: relative;
	}

	input {
		width: 100%;
		box-sizing: border-box;
		padding: 0.7rem 0.75rem;
		font: inherit;
		font-size: 1rem;
		color: var(--fg);
		background: var(--input-bg);
		border: 1px solid var(--border);
		border-radius: 10px;
	}

	input:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 1px;
	}

	.spinner {
		position: absolute;
		top: 50%;
		right: 0.7rem;
		width: 14px;
		height: 14px;
		margin-top: -7px;
		border: 2px solid var(--border);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation-duration: 3s;
		}
	}

	.results {
		position: absolute;
		z-index: 500;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		margin: 0;
		padding: 0.25rem;
		list-style: none;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: 0 10px 30px rgb(0 0 0 / 0.18);
		max-height: 17rem;
		overflow-y: auto;
	}

	.results button {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		width: 100%;
		padding: 0.5rem;
		text-align: left;
		font: inherit;
		background: none;
		border: 0;
		border-radius: 7px;
		color: var(--fg);
		cursor: pointer;
	}

	.results button:hover,
	.results button.highlighted {
		background: var(--hover);
	}

	.rname {
		font-size: 0.95rem;
	}

	.raddr {
		font-size: 0.78rem;
		color: var(--muted);
	}

	.empty {
		padding: 0.5rem;
		font-size: 0.85rem;
		color: var(--muted);
	}

	.chosen {
		margin: 0;
		font-size: 0.78rem;
		color: var(--muted);
		overflow-wrap: anywhere;
	}

	.chosen.warn {
		color: var(--danger);
	}
</style>
