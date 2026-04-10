1. 定时拉取的main分支的最新代码，并更新当前分支
2. 将当前分支的.github路径下的所有改动切换到新的分支 enbale_xpu_ci，并且不保留commit history
3. 在新的干净的分支上，将amr,amr-registry,registry相关逻辑，字段清理干净
4. 将main分支上没有的“if: false”逻辑全部删掉