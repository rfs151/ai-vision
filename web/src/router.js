import { createRouter, createWebHashHistory } from 'vue-router'

// Hash history: the built app is served as static files by FastAPI, no server-side fallback needed
export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/wall' },
    { path: '/search', component: () => import('./views/Search.vue'), meta: { title: 'AI 查询' } },
    { path: '/wall', component: () => import('./views/Wall.vue'), meta: { title: '监控大屏' } },
    { path: '/devices', component: () => import('./views/Devices.vue'), meta: { title: '设备管理' } },
    { path: '/rules/:id?', component: () => import('./views/Rules.vue'), meta: { title: '规则配置' } },
    { path: '/pose', component: () => import('./views/Pose.vue'), meta: { title: '姿态识别' } },
    { path: '/lab', component: () => import('./views/Lab.vue'), meta: { title: '视频实验室' } },
    { path: '/alarms', component: () => import('./views/Alarms.vue'), meta: { title: '告警中心' } },
    { path: '/docs/:doc?', component: () => import('./views/Docs.vue'), meta: { title: '开发文档' } },
    { path: '/settings', component: () => import('./views/Settings.vue'), meta: { title: '通知设置' } },
  ],
})
