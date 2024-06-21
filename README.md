# Andúril

https://lotr.fandom.com/wiki/Andúril

使用AI进行Code Review

## 安装和使用

.env 配置

```env
GIT_TOKEN_github.com="xxx"
GIT_TOKEN_gitlab.cn="xxx"
OPENAI_API_KEY="sk-xxx"

COZE_TOKENS="pat_x1,pat_x2,pat_xn"
COZE_BOT_IDS="7382754917288919075,7382765445776490536,7382768425716006975"

```

执行命令

```bash
python main.py run --prompt_version 2 --mr <merge_request_url>
```

## 后续规划

- [x] 支持扣子的API
- [ ] 支持github/gitcode/gitee等代码托管平台
- [ ] 支持WeCode的API
- [ ] 支持更多大模型接口
