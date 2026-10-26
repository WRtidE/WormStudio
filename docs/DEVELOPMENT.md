# 开发与验证

在仓库根目录、已安装依赖的Python环境运行：

```powershell
python run_tests.py
node --test tests/annotation-coordinates.test.js tests/test_annotation_readiness.cjs
$env:PYTHONPATH = 'packages/single-worm-methods'
python -m unittest discover -s packages/single-worm-methods/tests
Remove-Item Env:PYTHONPATH
```

应用测试使用合成视频、临时目录和受控的模型替身，测试质量边界、标注接口和指标一致性；不需要原工作区视频或权重。算法包只分发不依赖研究样例数据的合成测试，原样例回放测试保留在开发归档，不伪装为已在无数据发行包运行。

`.github/workflows/tests.yml`提供Linux CPU与JavaScript检查。其配置已随源码提供；上传前不声称远程CI已通过。当前发布整理是在已有Windows环境中验证，未完成干净机器GPU端到端验收。

改变几何、身份、头尾或资源路由时，增加能捕获真实错误的测试。不要为格式或文本修改添加镜像式测试。资源路由的新测试覆盖缺省Python、本机覆盖优先级、标准模型目录和内置算法包。

## 数据保护

不在源代码中加入个人路径、访问令牌、实验记录、权重或真实标注。不要为增加接受率放宽正式测量条件。修改算法签名会使缓存失效，不能为了复用旧缓存绕过校验。

如要提交复现实验材料，应单独记录数据权限、来源、标注状态、固定版本和失败结果；源码测试通过不等于算法精度验证。
