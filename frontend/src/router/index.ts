import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ProvenanceView from '@/views/ProvenanceView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/kg_provenance', name: 'kg_provenance', component: ProvenanceView, props: true },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
