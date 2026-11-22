# 国家队持仓对照看板 · 自动部署包

静态单文件看板，部署到 **GitHub Pages** 获得固定访问地址；GitHub Actions 按季度定时重建数据并发布，也支持手动触发。

## 一、目录结构

```
deploy/
├── build.py                        # 一键重建：拉数据 → 生成 JSON → 内联进 HTML
├── www/
│   ├── index.html                  # 看板页面（ECharts 走 jsDelivr CDN）
│   └── dashboard_data.json         # 数据包基线（断网/接口失败时保底）
└── .github/workflows/pages.yml      # 定时 + 手动部署工作流
```

## 二、首次上线步骤（需要你操作的部分）

> GitHub 账号只能本人注册，我无法代注册。

1. 注册免费 GitHub 账号：https://github.com/signup （邮箱验证即可，无需付费）。
2. 新建仓库：右上角 `New repository`，名字自取（例如 `national-team-dashboard`），选 **Public**，不要勾选初始化 README。
3. 把本 `deploy/` 目录里的 **全部内容** 上传到仓库根目录：
   - 网页端 `Add file → Upload files`，把 `build.py`、`www/`、`.github/` 一起拖进去提交；
   - 注意 `.github` 文件夹要在仓库根目录，不要多套一层 `deploy/`。
4. 仓库 `Settings → Pages`，**Source** 选择 **GitHub Actions**（不用配分支目录）。
5. 进入 `Actions` 标签页，选 `Deploy Dashboard`，点 `Run workflow` 手动跑一次。跑完后在 `Settings → Pages` 拿到固定地址，形如：
   `https://<用户名>.github.io/<仓库名>/`
   （页面为 `www/index.html`，Actions 发布后根地址即可访问；若 404，改用 `https://<用户名>.github.io/<仓库名>/www/index.html`。）

## 三、自动更新机制

- **定时**：每年 1/4/7/10 月 20 日 02:15 UTC 自动重建（季报披露窗口后）。GitHub 定时任务可能有几分钟到几十分钟延迟。
- **手动**：Actions 页 `Run workflow` 随时重新生成。
- **数据来源**：
  - 持仓快照 / 环比增减：东方财富数据中心 `RPT_NATIONAL_STATISTICS`；
  - 指数日线（上证、沪深300）：新浪行情接口；
  - 接口失败时自动保留基线数据包，不会让页面挂掉。
- 仅用 Python 标准库，Runner 上无需 `pip install`。

## 四、当前口径与已知缺口（重要）

### 已自动更新
- 最新持仓结构：家数、总市值、主体 / 板块 / 行业分布；
- 近两年 9 个季度的环比聚合：新进 / 加仓 / 不变 / 减仓 / 清仓；
- 行业资金净流入排行、分季度净流入趋势、主体→行业桑基；
- 指数近两年日线与分季度收益。

### 暂未接入自动重算（前端已隐藏对应面板）
以下模块的原始计算脚本此前未落盘，目前以基线快照保留，`btAvailable=false` 时页面已隐藏：
- 组合回测净值曲线（P1/P2/P4/P6 等组合）；
- 月度收益、分季度组合收益、区间累计绩效；
- 因子明细重算（PE / PB / 股息率 / 占流通比的行情源接入）。

**后续补全自动重算需要做的事：**
1. 接入个股行情 / 估值源（行业、板块、收盘价、PE、PB、股息率），东财快照接口本身不返回这些字段；
2. 从成品 CSV 反推回测引擎：成分筛选规则（剔银行 + 股息率≥4% + PB<1.5）、市值加权 / 等权 / 单票上限 10%、按交易日再平衡；
3. 重算完成后把数据包里的 `btAvailable` 改为 `true`，前端自动恢复回测面板。

### 口径提醒
- 行业 / 板块归属、因子值在历史快照上来自当前基线映射；后期已退出个股记为“其他”。
- 资金流净额是持仓市值变动口径，含股价被动涨跌，不等于纯申赎金额。
- 东财 / 新浪接口在 GitHub 海外 Runner 上若被限流或拦截，需要在 `build.py` 里补备用源或代理。

## 五、本地试跑

```bash
cd deploy
python3 build.py
# 产物在 www/，直接用浏览器打开 www/index.html 即可
```
