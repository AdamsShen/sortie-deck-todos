# Python Argument Unpacking: `*` and `**`

## 1. Where this comes from

While reading the parallel-wave orchestration in `graph.py`, we hit this line:

```python
results = await asyncio.gather(*[_run_branch(br["members"]) for br in branches])
```

The `*` is neither a pointer nor a wildcard — it is Python's **iterable unpacking operator (star-unpacking)**.

## 2. What `*` does

`*` **expands** the list comprehension that follows it, passing each element as a **positional argument** to `asyncio.gather`:

```python
results = await asyncio.gather(
    _run_branch(branches[0]["members"]),
    _run_branch(branches[1]["members"]),
    _run_branch(branches[2]["members"]),
    # ...
)
```

Without `*`, the whole list is passed as a single argument and `asyncio.gather` raises — it only accepts multiple positional arguments, not one list of coroutines.

Key points:

- `_run_branch` is `async def`, so calling it returns a **coroutine object** (nothing runs yet).
- The list comprehension builds one coroutine per branch.
- `*` flattens that list into individual positional arguments.
- Effect: **all branches run concurrently**; `gather` waits for all of them and returns results in the same order they were passed.

## 3. The four symmetric positions of `*` and `**`

`*` works on **positional arguments / iterables** (list, tuple, set, str…), while `**` works on **keyword arguments / dict**. They are perfectly symmetric:

| Position | `*` (iterable → positional) | `**` (dict → keyword) |
|----------|-----------------------------|------------------------|
| Function **call** | `f(*args)` unpack: list/tuple → positional args | `f(**d)` unpack: dict → keyword args |
| Function **definition** | `def f(*args)` pack: positional args → tuple | `def f(**kw)` pack: keyword args → dict |
| Literal | `[*a, *b]` merge lists; `{*a, *b}` merge sets | `{**d1, **d2}` merge dicts |

## 4. Examples

### 4.1 Unpacking at the call site

```python
def f(a, b, c):
    print(a, b, c)

f(*[1, 2, 3])   # same as f(1, 2, 3)   → 1 2 3
f(*(1, 2), 3)   # same as f(1, 2, 3)
```

### 4.2 What a dict looks like after `**` unpacking

`**` splits a dict into a set of `key=value` keyword arguments; the key becomes the parameter name (must be a string) and the value becomes its argument:

```python
{"a": 1, "b": 2, "c": 3}   →   a=1, b=2, c=3
```

```python
def f(a, b, c):
    print(a, b, c)

f(**{"a": 1, "b": 2, "c": 3})   # same as f(a=1, b=2, c=3)
```

Constraints:

```python
f(**{1: 1, 2: 2})                        # TypeError: keywords must be strings
f(**{"a": 1, "b": 2, "c": 3, "d": 4})    # TypeError: unexpected keyword argument 'd'
f(**{"a": 1, "b": 2})                    # TypeError: missing 1 required positional argument: 'c'
```

### 4.3 Packing at the definition site

```python
def g(*args, **kw):
    print(args, kw)

g(1, 2, 3, name="x")
# args == (1, 2, 3)     ← extra positional args packed into a tuple
# kw   == {"name": "x"} ← extra keyword args packed into a dict
```

### 4.4 Merging literals

```python
[*[1, 2], *[3, 4]]        # [1, 2, 3, 4]     merge lists
(*[1, 2], *[3, 4])        # (1, 2, 3, 4)
{*{1, 2}, *{3, 4}}        # {1, 2, 3, 4}     merge sets
{**{"a": 1}, **{"b": 2}}  # {'a': 1, 'b': 2} merge dicts (later key wins)
```

## 5. Core differences

|  | `*` | `**` |
|---|---|---|
| Operates on | Iterables (list/tuple/set/str…) | Mappings (dict; keys must be strings) |
| Expands into | Positional args `f(1, 2, 3)` | Keyword args `f(a=1, b=2)` |
| Packs into | `tuple` | `dict` |
| Carries names | No, order only | Yes, the key is the parameter name |

Direction summary:

- At a **call**, `**` splits `{"a": 1}` into `a=1`; at a **definition**, it collects `a=1` into `{"a": 1}` — exactly opposite directions.
- `*` is the same, except it works on ordered values instead of key/value pairs.

Mnemonic:

- `*` = “flatten into a sequence of values”, feeding positional args;
- `**` = “flatten into key=value pairs”, feeding keyword args.

## 6. Related code

```579:579:src/sortie_deck/graph.py
            results = await asyncio.gather(*[_run_branch(br["members"]) for br in branches])
```
