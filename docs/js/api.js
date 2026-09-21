/**
 * Genius — Shared API Client
 * ===========================
 * This is the ONE file that talks to the backend.
 * The website uses it. When a React Native app is built later,
 * this same logic (translated to fetch/axios) becomes its data layer too.
 *
 * Change API_BASE_URL to your Replit backend URL once deployed.
 */

const API_BASE_URL = window.GENIUS_API_URL || "http://localhost:8080"; // set in index.html

const GeniusAPI = {
  /**
   * Connect a new store — the onboarding step.
   */
  async onboardStore({ name, phone, system, plan, suppliers }) {
    const res = await fetch(`${API_BASE_URL}/onboard`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, phone, system, plan, suppliers }),
    });
    if (!res.ok) throw new Error(`Onboarding failed: ${res.status}`);
    return res.json();
  },

  /**
   * Upload a sales CSV for a store and get back Claude's analysis.
   */
  async analyzeSales(storeId, file) {
    const formData = new FormData();
    formData.append("store_id", storeId);
    formData.append("file", file);

    const res = await fetch(`${API_BASE_URL}/analyze`, {
      method: "POST",
      body: formData,
    });
    if (!res.ok) throw new Error(`Analysis failed: ${res.status}`);
    return res.json();
  },

  /**
   * Get all stores — used by the admin dashboard.
   */
  async listStores() {
    const res = await fetch(`${API_BASE_URL}/admin`);
    if (!res.ok) throw new Error(`Failed to load stores: ${res.status}`);
    return res.json();
  },

  /**
   * Get a Stripe checkout link for a store's subscription.
   */
  getSubscribeLink(storeId, plan) {
    return `${API_BASE_URL}/subscribe/${storeId}/${plan}`;
  },

  /**
   * Health check — confirms the backend is alive.
   */
  async ping() {
    const res = await fetch(`${API_BASE_URL}/`);
    return res.json();
  },
};

// Make it available globally for the demo pages,
// and exportable for future bundler-based apps (React Native, etc.)
window.GeniusAPI = GeniusAPI;
if (typeof module !== "undefined" && module.exports) {
  module.exports = GeniusAPI;
}
