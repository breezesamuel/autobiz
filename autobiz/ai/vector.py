"""AutoBiz 向量/几何/聚类核心算法模块。

不依赖 numpy/scipy，纯 Python 实现，保证能在空环境跑。
每个算法都挂进业务：不是理论，跑完能直接给 pipeline 用。
"""
import math
import re
from collections import Counter, defaultdict
from typing import List, Dict, Tuple


# ============================================================
# 1. 文本向量化 —— TF-IDF 极简实现
# ============================================================

_STOPWORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with","by",
    "is","are","was","were","be","been","being","have","has","had","do","does",
    "did","will","would","should","could","can","may","might","must","shall",
    "not","no","nor","so","as","if","then","than","also","from","into","over",
    "under","about","above","below","between","up","down","out","off","too",
    "very","just","only","all","any","each","every","both","few","more","most",
    "other","some","such","than","too","very","s","t","re","ve","ll","d","m",
}

def tokenize(text: str) -> List[str]:
    """小写 + 字母数字分词 + 去停用词 + 去短词。"""
    if not text: return []
    low = text.lower()
    tokens = re.findall(r"[a-z][a-z0-9+#.\-]{1,}", low)
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]


def build_tfidf(docs: List[str]) -> Tuple[List[List[float]], Dict[str, int]]:
    """极简 TF-IDF：把 N 篇文档 → N×V 稠密矩阵 + 词表。
    
    公式: tf * log(N / (1 + df))
    归一化: L2 normalize 每个向量
    """
    # 1. 分词
    tok_docs = [tokenize(d) for d in docs]
    
    # 2. 词表
    vocab = {}
    for toks in tok_docs:
        for t in toks:
            if t not in vocab:
                vocab[t] = len(vocab)
    V = len(vocab)
    if V == 0: return [], {}
    
    # 3. IDF
    N = len(tok_docs)
    df = Counter()
    for toks in tok_docs:
        for t in set(toks):
            df[t] += 1
    idf = {t: math.log(N / (1 + df[t])) for t in df}
    
    # 4. TF-IDF 矩阵
    matrix = []
    for toks in tok_docs:
        vec = [0.0] * V
        tf = Counter(toks)
        for t, cnt in tf.items():
            j = vocab[t]
            vec[j] = cnt * idf[t]
        # L2 normalize
        norm = math.sqrt(sum(x*x for x in vec))
        if norm > 0:
            vec = [x/norm for x in vec]
        matrix.append(vec)
    
    return matrix, vocab


def cosine_sim(a: List[float], b: List[float]) -> float:
    """余弦相似度。假设已 L2 normalize，直接点积。"""
    return sum(x*y for x, y in zip(a, b))


# ============================================================
# 2. K-Means 聚类（纯 Python Lloyd）
# ============================================================

def _euclid(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x-y)*(x-y) for x, y in zip(a, b)))


def _random_init(vecs: List[List[float]], k: int, seed: int = 42) -> List[List[float]]:
    """k-means++ 初始化 (简化版: 先拿 k 个等距点当初始中心)。"""
    import random
    rng = random.Random(seed)
    n = len(vecs)
    if n == 0: return []
    if k >= n: return list(vecs)
    # 先拿最远的一对点
    i0 = rng.randrange(n)
    c0 = list(vecs[i0])
    centers = [c0]
    # 依次选距离已有中心最远的点
    while len(centers) < k:
        max_min_dist = -1.0
        best_idx = -1
        for i in range(n):
            if any(vecs[i] == c for c in centers): continue
            d = min(_euclid(vecs[i], c) for c in centers)
            if d > max_min_dist:
                max_min_dist = d
                best_idx = i
        centers.append(list(vecs[best_idx]))
    return centers


def kmeans(vecs: List[List[float]], k: int, max_iter: int = 30,
           seed: int = 42) -> Tuple[List[int], List[List[float]], float]:
    """纯 Python K-Means Lloyd 算法。
    
    返回: (labels, centers, inertia)
    """
    n = len(vecs)
    if n == 0: return [], [], 0.0
    k = min(k, n)
    centers = _random_init(vecs, k, seed)
    labels = [0] * n
    
    for _iter in range(max_iter):
        # 分配点到最近中心
        new_labels = []
        for v in vecs:
            best, best_d = 0, float("inf")
            for j, c in enumerate(centers):
                d = _euclid(v, c)
                if d < best_d:
                    best_d = d
                    best = j
            new_labels.append(best)
        
        # 更新中心 (均值)
        new_centers = [[] for _ in range(k)]
        sums = [[0.0]*len(vecs[0]) for _ in range(k)]
        cnts = [0]*k
        for v, lbl in zip(vecs, new_labels):
            for j, x in enumerate(v):
                sums[lbl][j] += x
            cnts[lbl] += 1
        for j in range(k):
            if cnts[j] == 0:
                new_centers[j] = centers[j]  # 空簇不动
            else:
                new_centers[j] = [s/cnts[j] for s in sums[j]]
        
        if new_labels == labels:
            break
        labels = new_labels
        centers = new_centers
    
    # inertia = 所有点到自己中心的距离平方
    inertia = sum(_euclid(v, centers[l])**2 for v, l in zip(vecs, labels))
    return labels, centers, inertia


# ============================================================
# 3. 几何覆盖贪心（Submodular Maximization）
# ============================================================

def geometric_coverage(vecs: List[List[float]], budget: int) -> List[int]:
    """选 budget 个向量，使得它们在欧氏空间覆盖面积最大（贪心）。
    
    应用到业务: 选 10 个客户提案，最大化"覆盖 pipeline 里所有需求的多样性"。
    贪心保证 (1 - 1/e) ≈ 63% 近似界 (submodular 经典结论)。
    """
    n = len(vecs)
    if n == 0 or budget <= 0: return []
    budget = min(budget, n)
    
    selected = []
    remaining = set(range(n))
    
    # 第一步: 选离原点最远的（最极端的需求画像）
    farthest = max(remaining, key=lambda i: math.sqrt(sum(x*x for x in vecs[i])))
    selected.append(farthest)
    remaining.remove(farthest)
    
    while len(selected) < budget:
        # 选到已选集合的"最小距离"最大的那个
        def score(i):
            return min(_euclid(vecs[i], vecs[s]) for s in selected)
        best = max(remaining, key=score)
        selected.append(best)
        remaining.remove(best)
    
    return selected


# ============================================================
# 4. 分位数归一化（替代手调阶梯权重）
# ============================================================

def quantile_rank(values: List[float], v: float) -> float:
    """返回 v 在 values 里的分位数 rank (0-1)。
    例: 分位数 0.9 的预算意思是"比 90% 的线索都高"。
    """
    if not values: return 0.0
    if v <= min(values): return 0.0
    if v >= max(values): return 1.0
    cnt = sum(1 for x in values if x <= v)
    return round(cnt / len(values), 4)


def percentile_buckets(values: List[float]) -> List[Tuple[float, float]]:
    """返回 (分位数阈值, 分值) 列表，用作评分阶梯。
    例: [(0.5, 18), (0.75, 24), (0.9, 30), (0.97, 35)]
    自动根据数据分布生成。
    """
    if not values: return []
    vs = sorted(values)
    n = len(vs)
    percentiles = [0.50, 0.65, 0.80, 0.90, 0.97]  # 5 个分桶
    out = []
    for p in percentiles:
        idx = min(int(p * n), n - 1)
        out.append((vs[idx], p))
    return out


# ============================================================
# 5. Pareto 前沿（定价优化）
# ============================================================

def pareto_frontier(points: List[Tuple[float, float, str]]) -> List[Tuple[float, float, str]]:
    """简单 Pareto 前沿（最大化双目标）。
    
    points: [(目标1值, 目标2值, 标签), ...]
    返回: 非支配点集 (两个目标都不比别的点差)
    
    应用: 定价时我们想同时最大化「我们的利润」和「客户的效用」
    → Pareto 前沿上的每一点都是最优报价，没有一个点在两个目标上同时输给另一个。
    """
    pareto = []
    for i, p in enumerate(points):
        dominated = False
        for j, q in enumerate(points):
            if i == j: continue
            # q 支配 p: q.1 >= p.1 且 q.2 >= p.2 且至少一个严格大于
            if (q[0] >= p[0] and q[1] >= p[1] and
                    (q[0] > p[0] or q[1] > p[1])):
                dominated = True
                break
        if not dominated:
            pareto.append(p)
    return pareto


# ============================================================
# 6. 对数效用模型（客户 vs 我们）
# ============================================================

def log_utility(our_profit: float, customer_budget: float,
                customer_option_value: float = 0.0,
                risk: float = 0.1) -> Dict[str, float]:
    """对数效用模型 —— 计算双赢空间。
    
    - 我们的效用: log(our_profit)，边际递减
    - 客户的效用: log(budget - our_profit) - risk_penalty
    - risk 是客户觉得"会不会被坑"的惩罚 (0-1)
    
    返回: 报价是否在 Pareto 最优区间 + 双方效用 + 推荐调价方向
    """
    if customer_budget <= 0:
        return {"ok": False, "error": "无预算无法建模"}
    
    # 我们的效用 (保底 $50)
    profit = max(our_profit, 50)
    util_us = math.log(profit)
    
    # 客户的效用: log(剩余预算) - 风险惩罚
    remaining = max(customer_budget - our_profit, 1)
    util_customer = math.log(remaining) - risk
    
    # 总效用 (合作剩余)
    util_total = util_us + util_customer
    
    # 优化方向: 看谁的效用对价格更敏感 (导数)
    # du_us/dp = 1/p, du_cust/dp = -1/(B-p)
    d_us = 1.0 / profit
    d_cust = -1.0 / max(customer_budget - our_profit, 1)
    
    # 如果客户的负导数绝对值更大 → 客户更敏感 → 应该降价
    sensitivity = abs(d_cust) - d_us
    if sensitivity > 0.3:
        direction = "down"  # 降价
    elif sensitivity < -0.3:
        direction = "up"    # 涨价
    else:
        direction = "ok"    # 平衡
    
    return {
        "ok": True,
        "our_profit": round(profit, 2),
        "util_us": round(util_us, 3),
        "util_customer": round(util_customer, 3),
        "util_total": round(util_total, 3),
        "direction": direction,
        "sensitivity": round(sensitivity, 3),
        "log_range_us": f"[{math.log(50):.1f}, {math.log(customer_budget):.1f}]",
    }


# ============================================================
# 7. 主入口: 对 pipeline 里的 leads 做全向量分析
# ============================================================

def analyze_leads(leads: List[dict], top_k_clusters: int = 5,
                  diversity_budget: int = 10) -> dict:
    """对一批 leads 做向量+聚类+几何覆盖深度分析。
    
    返回: {
        "cluster_counts": {cluster_id: count},
        "cluster_top_keywords": {cluster_id: [kw1, kw2, ...]},
        "diverse_indices": [最具多样性的 top_k 条索引],
        "pareto_pricing": [(我们利润, 客户剩余, 报价)],
        "centroid_distances": [每条到质心的距离 (离群点检测)],
    }
    """
    if not leads:
        return {"ok": False, "error": "空 leads"}
    
    # 1. 向量化
    docs = [f"{l.get('title','')} {l.get('description','')}" for l in leads]
    matrix, vocab = build_tfidf(docs)
    if not matrix:
        return {"ok": False, "error": "向量化失败"}
    
    # 2. 聚类
    labels, centers, inertia = kmeans(matrix, k=top_k_clusters)
    cluster_counts = Counter(labels)
    
    # 3. 每个簇的 top keywords
    idf_inv = {j: w for w, j in vocab.items()}
    cluster_top = defaultdict(Counter)
    for lead_idx, lbl in enumerate(labels):
        text = tokenize(docs[lead_idx])
        for t in text:
            cluster_top[lbl][t] += 1
    cluster_keywords = {}
    for lbl in sorted(cluster_top):
        kws = [w for w, _ in cluster_top[lbl].most_common(8)
               if w not in ("python", "javascript", "company", "work", "experience")]
        cluster_keywords[lbl] = kws[:5]
    
    # 4. 几何覆盖: 选 diversity_budget 个最具多样性的
    diverse = geometric_coverage(matrix, budget=min(diversity_budget, len(leads)))
    
    # 5. 离群点检测: 每条到质心的距离
    centroid_dist = []
    for i, v in enumerate(matrix):
        dists = [_euclid(v, c) for c in centers]
        centroid_dist.append(min(dists))
    
    return {
        "ok": True,
        "total_leads": len(leads),
        "vocab_size": len(vocab),
        "n_clusters": top_k_clusters,
        "cluster_counts": dict(cluster_counts),
        "cluster_top_keywords": cluster_keywords,
        "diverse_indices": diverse,
        "centroid_distances": centroid_dist,
        "inertia": round(inertia, 3),
        "_labels": labels,  # dashboard 用
    }


# ============================================================
# 8. 图算法 —— 相似度图 + Label Propagation + PageRank
# ============================================================

def build_similarity_graph(vecs: List[List[float]], threshold: float = 0.35) -> Dict[int, List[int]]:
    """把向量集 → 相似度图 (无向)。边 = cosine_sim ≥ threshold。
    
    稀疏化: 每个点只保留 top-k 邻居 (k=20)，避免 O(N²) 炸内存。
    """
    n = len(vecs)
    graph: Dict[int, List[int]] = {i: [] for i in range(n)}
    if n == 0:
        return graph
    k = min(20, max(3, n // 10))  # 每个点保留 top-k 邻居
    
    for i in range(n):
        sims = []
        for j in range(n):
            if i == j: continue
            s = cosine_sim(vecs[i], vecs[j])
            if s >= threshold:
                sims.append((s, j))
        sims.sort(reverse=True)
        for s, j in sims[:k]:
            graph[i].append(j)
    return graph


def label_propagation(graph: Dict[int, List[int]], max_iter: int = 30) -> List[int]:
    """Label Propagation 社区发现 (无监督)。
    
    业务意义: 发现 leads 里的"隐藏群体"——同一种需求画像的客户群。
    聚类用的是欧氏距离，LP 用的是图连通性 + 邻居投票，互补。
    """
    nodes = sorted(graph.keys())
    labels = {i: i for i in nodes}  # 初始标签 = 自身 ID
    
    for _ in range(max_iter):
        changed = False
        import random
        rng = random.Random(42)
        order = nodes[:]
        rng.shuffle(order)
        
        for i in order:
            neighbors = graph.get(i, [])
            if not neighbors:
                continue
            # 邻居中最多出现的标签
            vote: Dict[int, int] = {}
            for nb in neighbors:
                lbl = labels.get(nb, nb)
                vote[lbl] = vote.get(lbl, 0) + 1
            best_lbl = max(vote, key=vote.get)
            if labels[i] != best_lbl:
                labels[i] = best_lbl
                changed = True
        
        if not changed:
            break
    
    # 把标签归一化到 0..C-1
    unique = sorted(set(labels.values()))
    remap = {old: new for new, old in enumerate(unique)}
    return [remap[labels[i]] for i in nodes]


def pagerank(graph: Dict[int, List[int]], damping: float = 0.85,
             max_iter: int = 50, tol: float = 1e-5) -> List[float]:
    """纯 Python PageRank —— 找图中的"中心节点"。
    
    业务意义: PageRank 高的 lead = 被最多相似 lead 指向 = "同群里的 KOL"。
    优先攻 PR 高的 → 攻下一个能辐射整个社区。
    """
    nodes = sorted(graph.keys())
    n = len(nodes)
    if n == 0:
        return []
    idx = {node: i for i, node in enumerate(nodes)}
    
    # 初始化
    pr = [1.0 / n] * n
    
    # 预计算出链
    out_deg = {node: len(graph.get(node, [])) for node in nodes}
    
    for _ in range(max_iter):
        new_pr = [(1 - damping) / n] * n
        max_diff = 0.0
        for i, node in enumerate(nodes):
            for nb in graph.get(node, []):
                j = idx[nb]
                if out_deg[nb] > 0:
                    new_pr[j] += damping * pr[i] / out_deg[nb]
        for i in range(n):
            max_diff = max(max_diff, abs(new_pr[i] - pr[i]))
        pr = new_pr
        if max_diff < tol:
            break
    
    return pr


def bridge_score(graph: Dict[int, List[int]], labels: List[int]) -> List[float]:
    """桥接分数 —— 跨社区连接数。
    
    业务意义: bridge_score 高的 lead = 连接不同需求社区 = "跨界机会"。
    跨界机会通常竞争少、溢价高。
    """
    nodes = sorted(graph.keys())
    n = len(nodes)
    label_of = {nodes[i]: labels[i] for i in range(n)}
    scores = []
    
    for i, node in enumerate(nodes):
        my_lbl = labels[i]
        cross = 0
        total = 0
        for nb in graph.get(node, []):
            total += 1
            if label_of.get(nb, my_lbl) != my_lbl:
                cross += 1
        scores.append(cross / total if total > 0 else 0.0)
    
    return scores


# ============================================================
# 9. 贝叶斯关键词权重（Beta-Binomial Conjugate Prior）
# ============================================================

def bayesian_keyword_update(
    keyword_hits: List[List[str]],
    outcomes: List[int],  # 0=未转化, 1=转化
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
) -> Dict[str, Dict[str, float]]:
    """用历史转化数据做贝叶斯更新 —— 每个关键词的真实转化率。
    
    共轭先验: Beta(α, β) → Binomial → 后验 Beta(α+H, β+T-H)
    后验均值 = (α+H) / (α+β+T)
    
    业务: 哪些关键词历史上真的转化高？动态调 scorer 权重。
    prior_alpha=prior_beta=1 → 均匀先验 (无信息)
    """
    if not keyword_hits or not outcomes:
        return {}
    
    # 统计每个关键词的曝光 T 和转化 H
    total_hits = 0
    kw_stats: Dict[str, List[int]] = {}  # kw → [T, H]
    for hits, outcome in zip(keyword_hits, outcomes):
        for kw in set(hits):
            if kw not in kw_stats:
                kw_stats[kw] = [0, 0]
            kw_stats[kw][0] += 1  # T
            total_hits += 1
            if outcome == 1:
                kw_stats[kw][1] += 1  # H
    
    # 后验
    result = {}
    for kw, (t, h) in kw_stats.items():
        post_alpha = prior_alpha + h
        post_beta = prior_beta + (t - h)
        posterior_mean = post_alpha / (post_alpha + post_beta)
        # 95% 置信区间 (正态近似)
        var = post_alpha * post_beta / ((post_alpha + post_beta)**2 * (post_alpha + post_beta + 1))
        std = math.sqrt(var)
        lower = max(0.0, posterior_mean - 1.96 * std)
        upper = min(1.0, posterior_mean + 1.96 * std)
        
        result[kw] = {
            "exposure": t,
            "conversions": h,
            "conversion_rate": round(h / t, 4) if t > 0 else 0.0,
            "posterior_mean": round(posterior_mean, 4),
            "ci_95": [round(lower, 4), round(upper, 4)],
            "alpha_post": post_alpha,
            "beta_post": post_beta,
            "sample_size": t,
        }
    
    return result


# ============================================================
# 10. Isolation Forest 异常检测（纯 Python）
# ============================================================

def _iTree_build(samples: List[List[float]], depth: int, max_depth: int,
                 rng_seed: int) -> dict:
    """递归构建一棵 Isolation Tree."""
    import random
    rng = random.Random(rng_seed + depth * 137)
    n = len(samples)
    
    if depth >= max_depth or n <= 1:
        return {"type": "leaf", "size": n}
    
    # 随机选一个特征
    dim = len(samples[0])
    q = rng.randrange(dim)
    
    # 该特征的 min/max
    vals = [s[q] for s in samples]
    lo, hi = min(vals), max(vals)
    
    if lo == hi:
        return {"type": "leaf", "size": n}
    
    # 随机分割点
    p = rng.uniform(lo, hi)
    
    left = [s for s in samples if s[q] < p]
    right = [s for s in samples if s[q] >= p]
    
    return {
        "type": "node",
        "q": q, "p": p,
        "left": _iTree_build(left, depth + 1, max_depth, rng_seed),
        "right": _iTree_build(right, depth + 1, max_depth, rng_seed),
        "size": n,
    }


def _iTree_path_len(tree: dict, point: List[float], depth: int = 0) -> float:
    """计算一个点在树里的路径长度."""
    if tree["type"] == "leaf":
        # c(n) = 2 * (ln(n-1) + 0.5772) - 2*(n-1)/n   (欧拉常数)
        size = tree["size"]
        if size <= 1:
            return depth
        c = 2 * (math.log(size - 1) + 0.5772) - 2 * (size - 1) / size
        return depth + c
    
    if point[tree["q"]] < tree["p"]:
        return _iTree_path_len(tree["left"], point, depth + 1)
    else:
        return _iTree_path_len(tree["right"], point, depth + 1)


def isolation_forest(vecs: List[List[float]], n_trees: int = 100,
                     sample_size: int = 256, seed: int = 42) -> List[float]:
    """纯 Python Isolation Forest —— 异常检测。
    
    返回: 每个点的 anomaly score (0-1, 越高越异常)
    score ≈ 0.5 → 正常; > 0.6 → 可疑异常
    
    业务意义: score 高的 lead = "反常好机会" —— 比同类需求明显好 (预算特别高/描述特别详细/渠道特别优质)。
    这类通常是高优先级突破口。
    """
    import random
    rng = random.Random(seed)
    n = len(vecs)
    if n == 0:
        return []
    
    sample_size = min(sample_size, n)
    max_depth = math.ceil(math.log2(sample_size))
    
    # 建 n_trees
    forest = []
    for t in range(n_trees):
        idx = rng.sample(range(n), sample_size)
        sub = [vecs[i] for i in idx]
        forest.append(_iTree_build(sub, 0, max_depth, seed + t))
    
    # c(n) —— 正常化因子
    c_n = 2 * (math.log(n - 1) + 0.5772) - 2 * (n - 1) / n if n > 2 else 1.0
    
    # 算每个点的平均路径长度
    scores = []
    for v in vecs:
        avg_path = sum(_iTree_path_len(tree, v) for tree in forest) / len(forest)
        # anomaly score = 2^(-avg_path / c_n)
        score = 2 ** (-avg_path / c_n) if c_n > 0 else 0.5
        scores.append(score)
    
    return scores


def local_outlier_factor(vecs: List[List[float]], k: int = 5) -> List[float]:
    """LOF 简化版 —— 局部离群因子。
    
    跟 Isolation Forest 互补: IF 抓全局异常, LOF 抓局部密度异常 (在某个局部邻域里离群)。
    """
    n = len(vecs)
    if n == 0:
        return []
    k = min(k, n - 1)
    
    # 预算距离矩阵 (稀疏: 只存 top-k)
    neighbors = []
    for i in range(n):
        dists = []
        for j in range(n):
            if i != j:
                dists.append((_euclid(vecs[i], vecs[j]), j))
        dists.sort()
        neighbors.append(dists[:k])
    
    # k-distance 每个点
    k_dist = [nb[-1][0] for nb in neighbors]
    
    # reachability distance
    lof = []
    for i in range(n):
        lrd_sum = 0.0
        for d_ij, j in neighbors[i]:
            reach = max(d_ij, k_dist[j])
            lrd_sum += reach
        lrd_i = k / lrd_sum if lrd_sum > 0 else 0.0
        
        # LOF_i = 平均(lrd_j) / lrd_i
        lrd_neighbors = 0.0
        for d_ij, j in neighbors[i]:
            lrd_sum_j = sum(max(d_qt, k_dist[qt]) for d_qt, qt in neighbors[j])
            lrd_j = k / lrd_sum_j if lrd_sum_j > 0 else 0.0
            lrd_neighbors += lrd_j
        
        avg_lrd_nb = lrd_neighbors / k
        lof.append(avg_lrd_nb / lrd_i if lrd_i > 0 else 1.0)
    
    return lof


# ============================================================
# 11. 多目标排序器 —— 约束优化 + Pareto 最优
# ============================================================

def multiobjective_rank(
    leads: List[dict],
    objectives: List[Tuple[str, str]],
    weights: List[float] = None,
    constraints: Dict[str, Tuple[float, float]] = None,
) -> List[Tuple[int, float]]:
    """多目标排序 —— 把多个目标联合优化。
    
    objectives: [(字段名, 方向), ...]  方向: 'max' 越大越好, 'min' 越小越好
    weights: 每个目标的权重 (默认等权)
    constraints: {字段名: (min_val, max_val)} —— 硬约束
    
    业务: "我要预算高 AND 新鲜 AND 多样性 AND 离群点(反常好)"
    → 联合排序, 不是单一分数。
    
    返回: [(lead_index, 综合得分), ...] DESC 排序
    """
    n = len(leads)
    if n == 0:
        return []
    
    if weights is None:
        weights = [1.0 / len(objectives)] * len(objectives)
    
    # 提取并归一化每个目标
    obj_values = []
    for field, direction in objectives:
        vals = []
        for l in leads:
            v = l.get(field, 0) or 0
            vals.append(float(v))
        
        # min-max 归一化到 [0,1]
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            normed = [0.5] * n
        else:
            normed = [(v - lo) / (hi - lo) for v in vals]
        
        if direction == "min":
            normed = [1.0 - v for v in normed]
        
        obj_values.append(normed)
    
    # 硬约束过滤
    mask = [True] * n
    if constraints:
        for field, (lo_c, hi_c) in constraints.items():
            for i, l in enumerate(leads):
                v = float(l.get(field, 0) or 0)
                if v < lo_c or v > hi_c:
                    mask[i] = False
    
    # 加权求和
    results = []
    for i in range(n):
        if not mask[i]:
            results.append((i, -1.0))  # 约束外的排最后
            continue
        score = sum(w * obj_values[oi][i] for oi, w in enumerate(weights))
        results.append((i, score))
    
    results.sort(key=lambda x: -x[1])
    return results


def pareto_rank(leads: List[dict], fields: List[str]) -> List[int]:
    """Pareto 层排序 —— 真正的多目标非支配排序 (NSGA-II 简化版)。
    
    返回: 每个 lead 的 Pareto 层级 (0 = 最优, 数字越大越差)
    同一层 = 互不支配。比加权和更公平。
    """
    n = len(leads)
    if n == 0:
        return []
    
    # 提取值
    vals = []
    for l in leads:
        v = []
        for f in fields:
            v.append(float(l.get(f, 0) or 0))
        vals.append(v)
    
    # 找所有 Pareto 层
    dominated = [False] * n
    layers = []
    
    while sum(dominated) < n:
        layer = []
        for i in range(n):
            if dominated[i]:
                continue
            # 检查 i 是否被任何非支配点支配
            is_dominated = False
            for j in range(n):
                if i == j or dominated[j]:
                    continue
                # j 支配 i: j 所有维度 ≥ i 且至少一个 >
                all_ge = all(vals[j][d] >= vals[i][d] for d in range(len(fields)))
                any_gt = any(vals[j][d] > vals[i][d] for d in range(len(fields)))
                if all_ge and any_gt:
                    is_dominated = True
                    break
            if not is_dominated:
                layer.append(i)
        
        for i in layer:
            dominated[i] = True
        layers.append(layer)
    
    # 层级映射
    rank = [0] * n
    for layer_idx, layer in enumerate(layers):
        for i in layer:
            rank[i] = layer_idx
    
    return rank


# ============================================================
# 12. 深度分析主入口 —— 整合所有新算法
# ============================================================

def deep_analyze_leads(leads: List[dict],
                       objectives: List[Tuple[str, str]] = None,
                       constraints: Dict[str, Tuple[float, float]] = None,
                       label_prop_threshold: float = 0.30,
                       iforest_trees: int = 100) -> dict:
    """对一批 leads 做全景深度分析 (所有几何+图+异常+多目标)。
    
    返回 dict 包含:
      - communities: Label Propagation 社区标签
      - pagerank: 每个点的 PR 值 (越高越中心)
      - bridge_scores: 桥接分数 (越高越跨界)
      - anomaly_if: Isolation Forest 异常分
      - anomaly_lof: Local Outlier Factor
      - pareto_layers: Pareto 层排序 (0=最优)
      - ranked: 多目标加权排序 [(index, score), ...]
      - summary: 异常统计 + 社区统计
    """
    if not leads:
        return {"ok": False, "error": "空 leads"}
    
    # 1. 向量化
    docs = [f"{l.get('title','')} {l.get('description','')}" for l in leads]
    matrix, vocab = build_tfidf(docs)
    if not matrix:
        return {"ok": False, "error": "向量化失败"}
    
    n = len(matrix)
    
    # 2. 相似度图 + Label Propagation 社区
    graph = build_similarity_graph(matrix, threshold=label_prop_threshold)
    communities = label_propagation(graph)
    
    # 3. PageRank 中心性
    pr = pagerank(graph)
    
    # 4. 桥接分数 (跨社区)
    bridges = bridge_score(graph, communities)
    
    # 5. Isolation Forest 异常
    iforest_scores = isolation_forest(matrix, n_trees=iforest_trees)
    
    # 6. LOF 局部异常
    lof_scores = local_outlier_factor(matrix)
    
    # 7. 多目标排序
    if objectives is None:
        objectives = [
            ("score", "max"),
            ("budget_usd", "max"),
            ("freshness_hours", "min"),
        ]
    
    # 给 leads 加临时字段供多目标排序
    enriched = []
    for i, l in enumerate(leads):
        d = dict(l)
        # budget_usd: 用已有的 budget_max/min
        b = d.get("budget_max") or d.get("budget_min") or 0
        d["budget_usd"] = float(b) if b else 0.0
        # PR 和 bridge 作为排序目标
        d["pagerank"] = pr[i] if pr else 0.0
        d["bridge_score"] = bridges[i] if bridges else 0.0
        d["anomaly"] = iforest_scores[i] if iforest_scores else 0.5
        enriched.append(d)
    
    ranked = multiobjective_rank(enriched, objectives, constraints=constraints)
    pareto_layers = pareto_rank(enriched, [
        "score", "budget_usd", "pagerank", "bridge_score"
    ])
    
    # 8. 汇总统计
    from collections import Counter
    comm_counts = Counter(communities)
    anomaly_count = sum(1 for s in iforest_scores if s > 0.6)
    bridge_count = sum(1 for s in bridges if s > 0.3)
    pareto_top = sum(1 for r in pareto_layers if r == 0)
    
    return {
        "ok": True,
        "total": n,
        "vocab_size": len(vocab),
        
        # 社区
        "communities": communities,
        "community_counts": dict(comm_counts),
        "n_communities": len(comm_counts),
        
        # 图中心性
        "pagerank": [round(x, 4) for x in pr],
        "bridge_scores": [round(x, 4) for x in bridges],
        
        # 异常
        "anomaly_iforest": [round(x, 4) for x in iforest_scores],
        "anomaly_lof": [round(x, 4) for x in lof_scores],
        "anomaly_count": anomaly_count,
        "bridge_count": bridge_count,
        
        # 排序
        "ranked_indices": [i for i, _ in ranked],
        "ranked_scores": [round(s, 4) for _, s in ranked],
        "pareto_layers": pareto_layers,
        "pareto_front_size": pareto_top,
        
        # 辅助: 原始向量图 (dashboard 用)
        "_graph": graph,
    }


def demo() -> None:
    """快速自证: 跑 50 条 leads 全流程 (向量+聚类+图+异常+多目标)."""
    import sys; sys.path.insert(0, ".")
    from autobiz import db
    
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT * FROM leads
        WHERE status='shortlisted' OR score >= 40
        ORDER BY score DESC LIMIT 50
    """).fetchall()
    leads = [dict(r) for r in rows]
    
    print(f"📊 analyze_leads({len(leads)} leads) ...")
    result = analyze_leads(leads, top_k_clusters=5, diversity_budget=8)
    
    if not result.get("ok"):
        print(f"❌ {result.get('error')}"); return
    
    print(f"✅ vocab={result['vocab_size']}  inertia={result['inertia']}")
    print(f"\n📂 5 Clusters + Top Keywords:")
    for lbl in sorted(result["cluster_top_keywords"]):
        cnt = result["cluster_counts"].get(lbl, 0)
        kws = result["cluster_top_keywords"][lbl]
        print(f"  Cluster {lbl}: {cnt:4d} leads → {', '.join(kws)}")
    
    print(f"\n🎯 Top 8 Most Diverse Leads (几何覆盖):")
    for i, idx in enumerate(result["diverse_indices"]):
        l = leads[idx]
        dist = result["centroid_distances"][idx]
        print(f"  #{i+1} score={l.get('score') or 0:3d} "
              f"budget=${l.get('budget_max') or l.get('budget_min') or 0:>7} "
              f"dist={dist:.2f} "
              f"[{l.get('source','?')}] "
              f"{(l.get('title') or '')[:60]}")


if __name__ == "__main__":
    demo()
