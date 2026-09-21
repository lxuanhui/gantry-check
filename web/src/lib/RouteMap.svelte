<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { env } from '$env/dynamic/public';

	import type { Estimate, Gantry } from '$lib/api';
	import type { Place } from '$lib/onemap';
	import { decodePolyline } from '$lib/polyline';

	// Dynamic rather than static public env: an unset name is then '' instead of a build error,
	// so a fresh clone without web/.env still builds (with the no-key fallback below).
	const MAPS_KEY = env.PUBLIC_GOOGLE_MAPS_BROWSER_KEY ?? '';

	interface Props {
		origin: Place | null;
		destination: Place | null;
		estimate: Estimate | null;
		gantries: Gantry[];
	}

	let { origin, destination, estimate, gantries }: Props = $props();

	const SINGAPORE = { lat: 1.3521, lng: 103.8198 };
	const NO_KEY = 'Map unavailable: no Google Maps browser key configured.';
	const LOAD_FAILED = 'Map unavailable: the Google Maps API could not be loaded.';

	interface Libraries {
		core: google.maps.CoreLibrary;
		maps: google.maps.MapsLibrary;
		marker: google.maps.MarkerLibrary;
	}

	/** Anything drawn on the map: markers and polylines are both detached with `setMap(null)`. */
	interface Overlay {
		setMap(map: google.maps.Map | null): void;
	}

	let container: HTMLDivElement;
	// Google's objects are plain `let`s, never `$state`: Svelte 5's deep proxy would wrap the
	// API's class instances and break them. Only `status` is reactive, and it is what wakes
	// the redraw effect once the map exists.
	let libs: Libraries | null = null;
	let map: google.maps.Map | null = null;
	let info: google.maps.InfoWindow | null = null;
	let overlays: Overlay[] = [];
	let status = $state<'loading' | 'ready' | 'nokey' | 'error'>(
		MAPS_KEY ? 'loading' : 'nokey'
	);

	function clearOverlays() {
		for (const overlay of overlays) overlay.setMap(null);
		overlays = [];
		info?.close();
	}

	onMount(() => {
		// Without a key there is nothing to load: the map area shows a muted message instead,
		// and `npm run build` still prerenders (see the README).
		if (!browser || status === 'nokey') return;
		let disposed = false;

		// A rejected key does not reject importLibrary(): the API draws a grey map and calls
		// this hook instead, so the failure is only visible here.
		const global = window as Window & {
			gm_authFailure?: () => void;
			google?: { maps?: { importLibrary?: unknown } };
		};
		global.gm_authFailure = () => {
			if (!disposed) status = 'error';
		};

		void (async () => {
			try {
				// Loaded in the browser only — prerendering this page must still succeed.
				const { importLibrary, setOptions } = await import('@googlemaps/js-api-loader');
				// The API bootstraps once per page: on a client-side navigation back to this page
				// it is already installed, and setOptions() would only warn about the repeat.
				if (!global.google?.maps?.importLibrary) {
					setOptions({ key: MAPS_KEY, v: 'weekly' });
				}
				// `Marker` lives in the marker library; `Map`/`Polyline`/`InfoWindow` in maps and
				// `LatLngBounds`/`SymbolPath` in core.
				const [core, maps, marker] = await Promise.all([
					importLibrary('core'),
					importLibrary('maps'),
					importLibrary('marker')
				]);
				if (disposed) return;
				map = new maps.Map(container, {
					center: SINGAPORE,
					zoom: 11,
					// Matches the old map: the page scrolls over it rather than zooming.
					scrollwheel: false,
					clickableIcons: false,
					mapTypeControl: false,
					streetViewControl: false,
					fullscreenControl: false,
					zoomControl: true
				});
				info = new maps.InfoWindow();
				libs = { core, maps, marker };
				status = 'ready';
			} catch {
				if (!disposed) status = 'error';
			}
		})();

		return () => {
			disposed = true;
			clearOverlays();
			info = null;
			map = null;
			libs = null;
		};
	});

	/** A filled dot of a fixed pixel size, whatever the zoom (Leaflet's `circleMarker` look). */
	function dot(
		core: google.maps.CoreLibrary,
		options: { scale: number; fill: string; stroke: string; weight: number; fillOpacity: number }
	): google.maps.Symbol {
		return {
			path: core.SymbolPath.CIRCLE,
			scale: options.scale,
			fillColor: options.fill,
			fillOpacity: options.fillOpacity,
			strokeColor: options.stroke,
			strokeWeight: options.weight
		};
	}

	function escapeHtml(value: string): string {
		return value.replace(
			/[&<>"']/g,
			(character) =>
				({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character] ?? character
		);
	}

	// Redraw every overlay whenever the trip, the result or the gantry list changes.
	$effect(() => {
		// Read every reactive input up front so the effect re-runs on any of them.
		const state = status;
		const result = estimate;
		const from = origin;
		const to = destination;
		const allGantries = gantries;

		const currentMap = map;
		const loaded = libs;
		const bubble = info;
		if (state !== 'ready' || !currentMap || !loaded) return;
		const { core, maps, marker } = loaded;

		clearOverlays();
		const bounds = new core.LatLngBounds();
		let extended = 0;
		const extend = (lat: number, lng: number) => {
			bounds.extend({ lat, lng });
			extended += 1;
		};

		const encoded = result?.polyline;
		const path = encoded ? decodePolyline(encoded) : [];
		if (path.length > 1) {
			overlays.push(
				new maps.Polyline({
					path: path.map(([lat, lng]) => ({ lat, lng })),
					strokeColor: '#2563eb',
					strokeWeight: 5,
					strokeOpacity: 0.85,
					map: currentMap
				})
			);
			for (const [lat, lng] of path) extend(lat, lng);
		}

		const pin = (place: Place, colour: string) => {
			overlays.push(
				new marker.Marker({
					position: { lat: place.lat, lng: place.lng },
					title: place.name,
					icon: dot(core, { scale: 8, fill: colour, stroke: '#fff', weight: 3, fillOpacity: 1 }),
					map: currentMap
				})
			);
			extend(place.lat, place.lng);
		};

		if (from) pin(from, '#16a34a');
		if (to) pin(to, '#1d4ed8');

		// Only the gantries this trip actually crosses are drawn: charged ones highlighted,
		// free ones muted, everything else left off the map.
		const charged = new Map((result?.charges ?? []).map((charge) => [charge.gantry, charge]));
		if (charged.size) {
			for (const gantry of allGantries) {
				const charge = charged.get(gantry.number);
				if (!charge) continue;
				const paid = charge.amount_cents > 0;
				const spot = new marker.Marker({
					position: { lat: gantry.lat, lng: gantry.lng },
					title: `${gantry.number} · ${gantry.name}`,
					icon: dot(core, {
						scale: paid ? 8 : 6,
						fill: paid ? '#ef4444' : '#c9ced6',
						fillOpacity: paid ? 0.9 : 0.55,
						stroke: paid ? '#b91c1c' : '#8a8f98',
						weight: 2
					}),
					map: currentMap
				});
				// Explicit colour: the InfoWindow is always white, but its content inherits the page's
				// text colour, which is near-white in dark mode.
				const content = `<div style="color:#15181d"><strong>${escapeHtml(gantry.number)} · ${escapeHtml(gantry.name)}</strong><br>${charge.crossed_at.slice(11, 16)} — ${escapeHtml(charge.amount)}</div>`;
				spot.addListener('click', () => {
					bubble?.setContent(content);
					bubble?.open({ map: currentMap, anchor: spot });
				});
				overlays.push(spot);
				extend(gantry.lat, gantry.lng);
			}
		}

		if (extended > 1) {
			currentMap.fitBounds(bounds, 28);
		} else if (extended === 1) {
			currentMap.setCenter(bounds.getCenter());
			currentMap.setZoom(15);
		} else {
			currentMap.setCenter(SINGAPORE);
			currentMap.setZoom(11);
		}
	});
</script>

<div class="map">
	<!-- The API owns this element, so the fallback message is a sibling laid over it rather
	     than a child Svelte would have to insert into Google's own markup. -->
	<div class="canvas" bind:this={container} role="application" aria-label="Route and gantry map"></div>
	{#if status === 'nokey' || status === 'error'}
		<p class="unavailable">{status === 'nokey' ? NO_KEY : LOAD_FAILED}</p>
	{/if}
</div>
{#if estimate && !estimate.polyline}
	<p class="note">This API build returns no route geometry, so only the end points are shown.</p>
{/if}

<style>
	.map {
		position: relative;
		height: 260px;
		border-radius: 12px;
		border: 1px solid var(--border);
		background: var(--hover);
		overflow: hidden;
		z-index: 0;
	}

	@media (min-width: 640px) {
		.map {
			height: 360px;
		}
	}

	.canvas {
		position: absolute;
		inset: 0;
	}

	.unavailable {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		margin: 0;
		padding: 0 1rem;
		text-align: center;
		font-size: 0.85rem;
		color: var(--muted);
		background: var(--hover);
	}

	.note {
		margin: 0.5rem 0 0;
		font-size: 0.78rem;
		color: var(--muted);
	}
</style>
