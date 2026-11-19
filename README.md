# 涂料批次控制系统 —— ISA-88 配方执行引擎 + 设备仿真

按批次控制标准(ISA-88)实现的配方执行引擎。配方里的逻辑不再写死在程序里:产品切换只是换一份配方数据,
四层结构、阶段状态机、保持传播规则、共享设备仲裁全部由引擎执行,设备层由确定性仿真代替。

```
paintbatch/
  recipe.py        配方模型:procedure -> unit procedure -> operation -> phase,
                   顺序 / 并行分支 / 选择分支 / 转移条件
  states.py        阶段标准状态机 + 命令合法性表(唯一事实来源)
  engine.py        执行引擎:步骤图解释器、阶段运行时、状态合成、资源仲裁、保持传播
  ops.py           设备操作积木(进料、转移、升温、搅拌、研磨、放空...)
  sim.py           设备仿真:物料平衡、温度、搅拌、阀门/泵/研磨机动作时间、卡阀/传感器失灵
  plant_layout.py  车间布局:两个反应釜、两个预混缸、两个调漆缸、两台研磨机、公共管线
  recipes.py       12 份配方(P-101 ... P-112)
  scenarios.py     12 个验收剧本(多批次、保持/重启/中止命令、故障注入)
  runner.py        剧本执行器(带谓词的时间脚本钩子)
  verify.py        验收校验:迁移表、状态合成、管线独占、物料平衡、命令合法性
  record.py        每批次批记录(自包含 HTML,可打印)
  ui.py            执行进度图(甘特图 + 配方树 + 设备状态)
  acceptance.py    命令行入口
tests/test_engine.py
```

## 快速开始

```bash
# 12 个验收剧本:12 份配方、多批次并行、保持/重启/中止、故障注入
python3 -m paintbatch.acceptance --out reports

# 每份配方单独跑一遍
python3 -m paintbatch.acceptance --all-recipes --out reports

# 单个剧本 / 单个配方
python3 -m paintbatch.acceptance --scenario S12 --out reports
python3 -m paintbatch.acceptance --recipe P-105 --out reports

# 实时进度页面(边跑边刷新)
python3 -m paintbatch.acceptance --serve 8000

# 单元测试
python3 -m unittest discover -s tests -v
```

产物在 `reports/` 下:每个剧本一份 `Sx-progress.html`(进度图)、`Sx-verification.json`,
每个批次一份 `records/batch-<批次号>.html`(批记录),以及 `acceptance-summary.json`。

## 1. 配方四层结构

* **procedure 程序** —— 一份配方的根,下挂若干 unit procedure。
* **unit procedure 单元程序** —— 一段工艺区段(反应、调漆、研磨、灌装)。
* **operation 操作** —— 单元程序里的一个操作(进料、升温)。
* **phase 阶段** —— **只有阶段对应设备上的真实动作**。

上三层之间的步骤用节点连接:

| 节点 | 作用 |
|---|---|
| 元素(下一个层级) | 顺序执行;元素结束后继续下一步 |
| `Parallel` | 并行分支,全部结束后继续 |
| `Choice` | 按条件选第一条成立的分支(例如按参数选研磨机) |
| `Transition` | 转移条件(`Always` / `After(秒)` / `When(谓词)` / `Param`) |
| `Acquire` / `Release` | 申请 / 释放共享资源 |

示例(`recipes.py` 里 P-104 的两条灌装支路):

```python
Parallel([
    [Acquire(["FILL-LINE"], priority=8),
     Operation("FILL-1", [fill_phase("FILL-1", "FINISH-1", "all")]),
     Release(["FILL-LINE"])],
    [Acquire(["FILL-LINE"], priority=9),
     Operation("FILL-2", [fill_phase("FILL-2", "FINISH-2", "all")]),
     Release(["FILL-LINE"])],
], name="fills"),
```

两条支路真的并行,但公共灌装线同一时刻只发给一条支路——见第 4 节。

## 2. 阶段状态机

状态和命令的合法性只写在 `states.py` 的 `TRANSITIONS` 表里,引擎、校验器、批记录共用这一份:

| 状态 | 合法命令 |
|---|---|
| 空闲 IDLE | START, RESET |
| 运行 RUNNING | HOLD, STOP, ABORT |
| 正在保持 HOLDING | STOP, ABORT |
| 已保持 HELD | RESTART, STOP, ABORT |
| 正在重启 RESTARTING | HOLD, STOP, ABORT |
| 正在停止 STOPPING | ABORT |
| 已停止 STOPPED | ABORT, RESET |
| 正在中止 ABORTING | (等待中止序列结束) |
| 已中止 ABORTED | RESET |
| 完成 COMPLETE | RESET |

另外还有两条"自动迁移":主操作序列跑完 `RUNNING -> COMPLETE`;保持/重启/停止/中止这些**写出来的序列**
跑完时 `HOLDING -> HELD`、`RESTARTING -> RUNNING`、`STOPPING -> STOPPED`、`ABORTING -> ABORTED`。

在非法状态下发的命令不会执行、不会改状态,而是作为 `command_rejected` 记进批记录(S11 剧本里专门发
了几条错误命令验证这一点)。

**保持/重启不是暂停/继续**,每个阶段自己写:

```python
Phase("COOK", "REACTOR-1", [Agitate(...), HeatTo(...), Agitate(..., 60)],
      on_hold   = [Heater("REACTOR-1", 0), StopAgitator("REACTOR-1"), WaitSeconds(1)],
      on_restart= [Agitate("REACTOR-1"), WaitSeconds(1)],
      on_abort  = [Drain("REACTOR-1", "WASTE-LINE")],
      propagation="operation")
```

`RUNNING -> HOLDING` 只是"命令已被接受":阶段真的把设备停到安全状态、保持序列跑完,才 `HOLDING -> HELD`。
重启同理。主程序的计时在保持期间冻结,重启后从原处继续。

## 3. 故障、保持传播、上层状态合成

阶段操作失败(卡阀、泵跳闸、温度传感器断线、超时……)时抛出 `OpFailed`:

1. 阶段自己进 `HOLDING -> HELD`,故障原因进批记录和报警;
2. 按该阶段写明的 `propagation` 决定谁跟着保持:
   * `self` —— 只有它自己;
   * `operation` —— 同一操作里的其它阶段;
   * `unit_procedure` —— 整个单元程序里的阶段;
   * `batch` —— 整个批次。

**上层状态永远由下层合成**,不存在"上层直接下令变保持"这回事。合成规则(`engine.aggregate_state`):
中止 > 停止 > 正在保持 > 已保持 > 正在重启 > 运行 > 完成/停止/中止(全部结束才成立)。
校验器每个采样点用**独立实现**重算一遍合成结果,与引擎报告的状态比对。

## 4. 设备:独占与共享、申请/排队/释放

* 设备(反应釜、研磨机等)和公共管线段(`LETDOWN` 进料总管、`ADD-LINE` 添加剂总管、`REACT-OUT`
  反应釜出料管、`MILL-LINE` 研磨线、`FILL-LINE` 灌装线、`WASTE-LINE` 放空线)都是**可申请资源**。
* `Acquire` 发起申请,仲裁按 **(配方里的优先级, 批次次序, 申请先后)** 排序;拿不到就一直等,不做隐式抢占。
  同一条线即使在同一批次的两条并行支路之间也严格互斥——这正是"两个批次抢同一条输送管线把料打混"的根因。
* 拿到后由食谱显式 `Release`;**批次中止时**引擎按安全顺序把还没释放的收回去:先停泵、再关阀、再放线、
  最后放设备,并把管线里的存料记进批记录。
* 三层保护:
  1. 资源仲裁(申请/排队/授予);
  2. 每次物理转移要先拿"管线占用令牌"(`begin_transfer`),同一时刻只允许一个转移;
  3. 管线自身记录每次转移的**时间段 + 物料 + 批次**,校验器证明任意两个批次的时段不重叠。

## 5. 设备仿真

`sim.py` 是确定性的(时间用整数毫秒,结果可复现):

* 物料:质量、温度、批次号、牌号,混合按质量加权;
* 温度:加热器功率、被动散热、按质量的热容;
* 搅拌:带提速时间的搅拌器,速度 0..1;
* 阀门:双向行程时间,卡阀后执行机构动作但阀位不动、位置反馈不一致;
* 泵/研磨机:启停斜坡、额定流量/转速,跳闸后无法启动;
* 传感器:断线(读数抛异常)或卡值(读数冻结)——控制逻辑读到的是"假值",这正是故障注入要验证的。

故障注入:`stick_valve`、`fail_pump`、`fail_machine`、`fail_temp_sensor(断线/卡值)`。

## 6. 验收

`python3 -m paintbatch.acceptance` 跑 12 个剧本:

| 剧本 | 内容 |
|---|---|
| S1 | 单批次 P-101 全程 |
| S2 | 3 个批次同时跑,争用所有公共管线 |
| S3 | 厨师过程中两次操作员保持/重启 |
| S4 | 进料阀卡死 -> 保持 -> 修好后重启 |
| S5 | 泵跳闸,按单元程序传播,修好后重启 |
| S6 | 温度传感器断线 -> 故障 -> 保持 -> 换传感器后重启 |
| S7 | 中途中止,放空序列,资源按安全顺序释放 |
| S8 | 只保持 B801,B802 不受影响 |
| S9 | 研磨机跳闸,研磨阶段保持,停在该状态等维修 |
| S10 | 停止一批后,下一批才能拿到同一台反应釜 |
| S11 | 非法命令(空重启、重复保持、运行中复位)被拒绝 |
| S12 | 4 个批次错峰,同时到达灌装线(原始事故场景) |

每个剧本结束后 `verify.py` 检查 8 项:

1. **管线独占**:任一时刻一条线最多一个持有者;任意两个批次的物料时段不重叠;
2. **迁移表**:每个阶段的状态迁移序列与期望表逐条比对(可按批次限定、可允许额外迁移);
3. **状态合成**:每个采样点上,批次状态必须等于下层阶段的独立重算结果;
4. **命令合法性**:所有迁移都必须在状态机表内;被接受的操作员命令必须真的产生了迁移;
5. **物料平衡**:管线记录的转移量与批次记录的转移量一致,没有负库存;
6. **批次产量**:完成的批次确实把产品送进了成品罐;
7. **最终状态**:与剧本声明一致;
8. **资源释放**:结束/中止的批次不得仍占着设备。

12 个剧本全部 PASS,12 份配方单独跑也全部 PASS。

## 7. 批记录与进度图

* **批记录** `reports/records/batch-<批次号>.html`:配方/版本/产品/批号/批次号、开始结束时间、最终状态;
  汇总;每个阶段的完整迁移表(时间+原因);操作员命令及接受/拒绝;每次物料转移(线、起止、kg、物料);
  资源申请/释放记录;故障与报警;终态工艺值;原始事件日志;本批次的校验结果。
* **进度图** `reports/Sx-progress.html`:每个批次一张甘特图(按状态着色,保持/重启/中止/故障/命令打点),
  配方树实时状态,各管线当前持有批次与在途物料,各罐质量/温度/搅拌,故障与命令列表,校验结果。
  `--serve` 模式可以在批次运行过程中反复刷新查看。
