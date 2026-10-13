# Python 参数解包：`*` 与 `**`

## 1. 缘起

阅读 `graph.py` 的并行波次编排时，出现这样一行：

```python
results = await asyncio.gather(*[_run_branch(br["members"]) for br in branches])
```

其中的 `*` 不是指针，也不是通配符，而是 Python 的**可迭代对象解包运算符（star-unpacking）**。

## 2. `*` 的含义

`*` 把后面的列表推导式**拆开**，逐个元素当成**位置参数**传给 `asyncio.gather`：

```python
results = await asyncio.gather(
    _run_branch(branches[0]["members"]),
    _run_branch(branches[1]["members"]),
    _run_branch(branches[2]["members"]),
    # ...
)
```

去掉 `*` 就是把「一整个列表」当成单个参数传入，`asyncio.gather` 会报错——它只接受多个位置参数，不接受一个 coroutine 列表。

要点：

- `_run_branch` 是 `async def`，调用它返回的是 **coroutine 对象**（此时尚未执行）。
- 列表推导式生成「每个分支一个 coroutine」的列表。
- `*` 把列表摊平成一个个位置参数。
- 效果：**所有分支并发执行**，`gather` 等它们全部完成后返回结果列表，顺序与传入顺序一致。

## 3. `*` 与 `**` 的四个对应位置

`*` 处理的是**位置参数 / 可迭代对象**（list、tuple、set、str…），`**` 处理的是**关键字参数 / dict**，两者完全对称：

| 位置 | `*`（可迭代 → 位置参数） | `**`（dict → 关键字参数） |
|------|--------------------------|--------------------------|
| 函数**调用** | `f(*args)` 拆包：list/tuple → 位置参数 | `f(**d)` 拆包：dict → 关键字参数 |
| 函数**定义** | `def f(*args)` 打包：位置参数 → tuple | `def f(**kw)` 打包：关键字参数 → dict |
| 字面量 | `[*a, *b]` 合并 list；`{*a, *b}` 合并 set | `{**d1, **d2}` 合并 dict |

## 4. 示例

### 4.1 调用端拆包

```python
def f(a, b, c):
    print(a, b, c)

f(*[1, 2, 3])   # 等价于 f(1, 2, 3)   → 1 2 3
f(*(1, 2), 3)   # 等价于 f(1, 2, 3)
```

### 4.2 `**` 拆解之后的样子

`**` 把字典拆成一组「`键=值`」的关键字参数；key 变成参数名（必须是字符串），value 变成对应的值：

```python
{"a": 1, "b": 2, "c": 3}   →   a=1, b=2, c=3
```

```python
def f(a, b, c):
    print(a, b, c)

f(**{"a": 1, "b": 2, "c": 3})   # 等价于 f(a=1, b=2, c=3)
```

约束：

```python
f(**{1: 1, 2: 2})                        # TypeError: keywords must be strings
f(**{"a": 1, "b": 2, "c": 3, "d": 4})    # TypeError: unexpected keyword argument 'd'
f(**{"a": 1, "b": 2})                    # TypeError: missing 1 required positional argument: 'c'
```

### 4.3 定义端打包

```python
def g(*args, **kw):
    print(args, kw)

g(1, 2, 3, name="x")
# args == (1, 2, 3)     ← 多余的位置参数打包成 tuple
# kw   == {"name": "x"} ← 多余的关键字参数打包成 dict
```

### 4.4 字面量合并

```python
[*[1, 2], *[3, 4]]        # [1, 2, 3, 4]     合并 list
(*[1, 2], *[3, 4])        # (1, 2, 3, 4)
{*{1, 2}, *{3, 4}}        # {1, 2, 3, 4}     合并 set
{**{"a": 1}, **{"b": 2}}  # {'a': 1, 'b': 2} 合并 dict（同名 key 后者覆盖前者）
```

## 5. 核心区别

|  | `*` | `**` |
|---|---|---|
| 操作对象 | 可迭代对象（list/tuple/set/str…） | 映射对象（dict，key 必须为字符串） |
| 展开成 | 位置参数 `f(1, 2, 3)` | 关键字参数 `f(a=1, b=2)` |
| 打包成 | `tuple` | `dict` |
| 是否带「名字」 | 否，只有顺序 | 是，key 即参数名 |

方向总结：

- `**` 在**调用**时把 `{"a": 1}` 拆成 `a=1`；在**定义**时把 `a=1` 收成 `{"a": 1}`——方向刚好相反。
- `*` 同理，只是作用于顺序值而非键值对。

记忆口诀：

- `*` = 「摊成一串值」，喂给位置参数；
- `**` = 「摊成键=值对」，喂给关键字参数。

## 6. 相关代码

```579:579:src/sortie_deck/graph.py
            results = await asyncio.gather(*[_run_branch(br["members"]) for br in branches])
```
