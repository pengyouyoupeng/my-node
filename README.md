# My FreeDomain Website

基于 **DigitalPlat FreeDomain** 免费域名与 **Vercel** 边缘网络构建的个人网站。

---

## 🌐 网站信息

* **线上地址**: [https://freekuaile.dpdns.org](https://freekuaile.dpdns.org)
* **备用地址**: [https://public-zeta-lime.vercel.app](https://public-zeta-lime.vercel.app)
* **域名**: `freekuaile.dpdns.org`
* **托管平台**: Vercel
* **HTTPS**: 自动 Let's Encrypt 证书支持

---

## 📁 目录结构

```text
.
├── README.md        # 项目说明
└── public/          # 网站源码目录
    ├── index.html   # 主页 HTML/CSS 源码
    └── .vercel/     # Vercel 部署配置
```

---

## 🚀 日常更新与部署

修改 `public/index.html` 或添加新页面后，在终端执行以下命令即可一键发布更新：

```bash
npx vercel deploy public --prod --yes
```
