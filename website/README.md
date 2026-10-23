# 网站前端

React + Vite + Three.js 前端，构建后由根目录的 `share_site` FastAPI 服务提供页面。生产部署保持同源，API 和论坛使用同一登录会话。

```bash
npm ci
npm run build
```

完整安装/启动见 [项目 README](../README.md)，交互与设计说明见 [DESIGN.md](DESIGN.md)，服务器配置见 [deploy/README.md](../deploy/README.md)。本地开发时后端监听 8800，再运行 `npm run dev`。
