<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import 'leaflet/dist/leaflet.css';
	import type { LayerGroup, Map as LeafletMap } from 'leaflet';
	import type { Estimate, Gantry } from '$lib/api';
	import type { Place } from '$lib/onemap';
	import { decodePolyline } from '$lib/polyline';

	interface Props {
		origin: Place | null;
		destination: Place | null;
		estimate: Estimate | null;
		gantries: Gantry[];
	}

	let { origin, destination, estimate, gantries }: Props = $props();

	const SINGAPORE: [number, number] = [1.3521, 103.8198];

	type Leaflet = typeof import('leaflet');

	// Leaflet 1.9's ESM build exports its members by name; some bundles hand back a CJS
	// interop namespace with everything under `default` instead.
	function resolveLeaflet(module: Leaflet): Leaflet {
		return (module as unknown as { default?: Leaflet }).default ?? module;
	}

	let container: HTMLDivElement;
	// Leaflet touches `window` at import time, so it is only ever loaded in the browser —
	// prerendering this page must still succeed.
	let leaflet = $state<Leaflet | null>(null);
	let map: LeafletMap | null = null;
	let overlay: LayerGroup | null = null;

	onMount(() => {
		if (!browser) return;
		let disposed = false;

		void (async () => {
			const module = await import('leaflet');
			if (disposed) return;
			const L = resolveLeaflet(module);
			map = L.map(container, { scrollWheelZoom: false }).setView(SINGAPORE, 11);
			L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
				maxZoom: 19,
				attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
			}).addTo(map);
			overlay = L.layerGroup().addTo(map);
			leaflet = module;
		})();

		return () => {
			disposed = true;
			map?.remove();
			map = null;
			overlay = null;
		};
	});

	function pin(L: Leaflet, kind: 'origin' | 'destination') {
		return L.divIcon({
			className: 'pin-wrap',
			html: `<span class="pin pin-${kind}"></span>`,
			iconSize: [18, 18],
			iconAnchor: [9, 9]
		});
	}

	// Redraw every overlay whenever the trip, the result or the gantry list changes.
	$effect(() => {
		const module = leaflet;
		const currentMap = map;
		const layer = overlay;
		if (!module || !currentMap || !layer) return;
		const L = resolveLeaflet(module);

		// Read every reactive input up front so the effect re-runs on any of them.
		const result = estimate;
		const from = origin;
		const to = destination;
		const allGantries = gantries;

		layer.clearLayers();
		const bounds: [number, number][] = [];

		const encoded = result?.polyline;
		const path = encoded ? decodePolyline(encoded) : [];
		if (path.length > 1) {
			L.polyline(path, { color: '#2563eb', weight: 5, opacity: 0.85 }).addTo(layer);
			bounds.push(...path);
		}

		if (from) {
			L.marker([from.lat, from.lng], { icon: pin(L, 'origin'), title: from.name }).addTo(layer);
			bounds.push([from.lat, from.lng]);
		}
		if (to) {
			L.marker([to.lat, to.lng], { icon: pin(L, 'destination'), title: to.name }).addTo(layer);
			bounds.push([to.lat, to.lng]);
		}

		// Only the gantries this trip actually crosses are drawn: charged ones highlighted,
		// free ones muted, everything else left off the map.
		const charged = new Map((result?.charges ?? []).map((charge) => [charge.gantry, charge]));
		if (charged.size) {
			for (const gantry of allGantries) {
				const charge = charged.get(gantry.number);
				if (!charge) continue;
				const paid = charge.amount_cents > 0;
				L.circleMarker([gantry.lat, gantry.lng], {
					radius: paid ? 8 : 6,
					weight: 2,
					color: paid ? '#b91c1c' : '#8a8f98',
					fillColor: paid ? '#ef4444' : '#c9ced6',
					fillOpacity: paid ? 0.9 : 0.55
				})
					.bindPopup(
						`<strong>${gantry.number} · ${gantry.name}</strong><br>${charge.crossed_at.slice(11, 16)} — ${charge.amount}`
					)
					.addTo(layer);
				bounds.push([gantry.lat, gantry.lng]);
			}
		}

		if (bounds.length > 1) {
			currentMap.fitBounds(L.latLngBounds(bounds), { padding: [28, 28] });
		} else if (bounds.length === 1) {
			currentMap.setView(bounds[0], 15);
		} else {
			currentMap.setView(SINGAPORE, 11);
		}
	});
</script>

<div class="map" bind:this={container} role="application" aria-label="Route and gantry map"></div>
{#if estimate && !estimate.polyline}
	<p class="note">This API build returns no route geometry, so only the end points are shown.</p>
{/if}

<style>
	.map {
		height: 260px;
		border-radius: 12px;
		border: 1px solid var(--border);
		background: var(--hover);
		z-index: 0;
	}

	@media (min-width: 640px) {
		.map {
			height: 360px;
		}
	}

	.note {
		margin: 0.5rem 0 0;
		font-size: 0.78rem;
		color: var(--muted);
	}

	/* Leaflet renders divIcon markup outside this component's scope. */
	:global(.pin) {
		display: block;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		border: 3px solid #fff;
		box-shadow: 0 1px 4px rgb(0 0 0 / 0.5);
	}

	:global(.pin-origin) {
		background: #16a34a;
	}

	:global(.pin-destination) {
		background: #1d4ed8;
	}

	:global(.leaflet-container) {
		font: inherit;
		background: var(--hover);
	}
</style>
