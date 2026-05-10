/* dijkstra_core.c — edge-pair Dijkstra with turn/signal/intersection/corridor penalties.
 *
 * State encoding (3 cases):
 *   state = 0                    => start state at start_node, no incoming edge
 *   state = 2*eid + 1            => arrived at edge_v[eid] via edge eid (from edge_u[eid])
 *   state = 2*eid + 2            => arrived at edge_u[eid] via edge eid (from edge_v[eid])
 *   total state count = 2*n_edges + 1
 *
 * Adjacency given in CSR form:
 *   adj_off[node]   = start index into adj_edge / adj_to
 *   adj_off[n_nodes] = total adjacency entries
 *
 * Build with:
 *   clang -O3 -ffast-math -shared -fPIC -o dijkstra_core.dylib dijkstra_core.c -lm
 */

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

typedef struct { double cost; int state; } HE;

typedef struct {
    HE *data;
    int size;
    int capacity;
} Heap;

static Heap *heap_create(int cap) {
    Heap *h = (Heap*)malloc(sizeof(Heap));
    h->data = (HE*)malloc(sizeof(HE) * cap);
    h->size = 0;
    h->capacity = cap;
    return h;
}

static void heap_destroy(Heap *h) {
    free(h->data);
    free(h);
}

static void heap_push(Heap *h, double cost, int state) {
    if (h->size >= h->capacity) {
        h->capacity *= 2;
        h->data = (HE*)realloc(h->data, h->capacity * sizeof(HE));
    }
    int i = h->size++;
    h->data[i].cost = cost;
    h->data[i].state = state;
    while (i > 0) {
        int p = (i - 1) >> 1;
        if (h->data[p].cost > h->data[i].cost) {
            HE t = h->data[p]; h->data[p] = h->data[i]; h->data[i] = t;
            i = p;
        } else break;
    }
}

static int heap_pop(Heap *h, double *cost, int *state) {
    if (h->size == 0) return 0;
    *cost = h->data[0].cost; *state = h->data[0].state;
    h->size--;
    if (h->size > 0) {
        h->data[0] = h->data[h->size];
        int i = 0;
        for (;;) {
            int l = 2 * i + 1, r = 2 * i + 2, s = i;
            if (l < h->size && h->data[l].cost < h->data[s].cost) s = l;
            if (r < h->size && h->data[r].cost < h->data[s].cost) s = r;
            if (s != i) {
                HE t = h->data[s]; h->data[s] = h->data[i]; h->data[i] = t;
                i = s;
            } else break;
        }
    }
    return 1;
}

static inline double turn_cos_c(
    const double *lon, const double *lat,
    int prev_from, int u, int v
) {
    double mid_lat = (lat[prev_from] + lat[v]) * 0.5;
    double cl = cos(mid_lat * M_PI / 180.0);
    double ix = (lon[u] - lon[prev_from]) * cl;
    double iy =  lat[u] - lat[prev_from];
    double ox = (lon[v] - lon[u]) * cl;
    double oy =  lat[v] - lat[u];
    double n1 = sqrt(ix * ix + iy * iy);
    double n2 = sqrt(ox * ox + oy * oy);
    if (n1 < 1e-12 || n2 < 1e-12) return 1.0;
    return (ix * ox + iy * oy) / (n1 * n2);
}

/*
 * Mark all graph nodes that lie within `radius_m` of any node listed in
 * out_indices[]. Uses an equirectangular projection scaled by cos(lat0).
 * Writes a 0/1 flag to out_flag (length n_nodes); zeros are written first.
 */
void corridor_mark(
    int n_nodes,
    const double *node_lon, const double *node_lat,
    const int *out_indices, int n_out,
    const int *exclude_indices, int n_exclude,
    double radius_m,
    double cos_lat0,
    int *out_flag
) {
    for (int i = 0; i < n_nodes; i++) out_flag[i] = 0;
    if (radius_m <= 0.0 || n_out == 0) return;
    double r2 = radius_m * radius_m;
    double meters_per_deg_lat = 111320.0;
    double meters_per_deg_lon = 111320.0 * cos_lat0;

    for (int oi = 0; oi < n_out; oi++) {
        int idx = out_indices[oi];
        double olon = node_lon[idx];
        double olat = node_lat[idx];
        for (int k = 0; k < n_nodes; k++) {
            if (out_flag[k]) continue;            /* already marked */
            double dx = (node_lon[k] - olon) * meters_per_deg_lon;
            double dy = (node_lat[k] - olat) * meters_per_deg_lat;
            if (dx * dx + dy * dy < r2) out_flag[k] = 1;
        }
    }
    for (int i = 0; i < n_exclude; i++) {
        int idx = exclude_indices[i];
        if (idx >= 0 && idx < n_nodes) out_flag[idx] = 0;
    }
}


/*
 * Returns: number of states relaxed.
 * Outputs are written into caller-supplied arrays of length 2*n_edges + 1.
 *  - out_dist[s]      = best cost to state s, or HUGE_VAL if not reached
 *  - out_prev[s]      = previous state id, or -1
 *  - out_real[s]      = total real metres travelled to reach state s
 *  - out_heat_w[s]    = sum of heat * length along the path
 *  - out_heat_len[s]  = sum of length along the path (== out_real[s])
 *  - *out_target_state= -1 or best state id at target_node (if target_node >= 0)
 */
int dijkstra_run(
    int n_nodes, int n_edges,
    const int    *edge_u,    const int    *edge_v,
    const double *edge_len,  const double *edge_heat,
    const double *node_lon,  const double *node_lat,
    const int    *adj_off,   const int    *adj_edge, const int *adj_to,
    const int    *signal_flag,
    const int    *node_degree,
    int           start_node,
    int           target_node,            /* -1 = full search */
    double        alpha,
    double        turn_pen_m,
    double        signal_pen_m,
    double        inter_pen_m,
    const int    *penalised_edge_flag,    /* may be NULL */
    const int    *corridor_flag,          /* may be NULL */
    double        corridor_mult,
    double        max_cost,               /* HUGE_VAL for unbounded */

    double *out_dist, int *out_prev,
    double *out_real, double *out_heat_w, double *out_heat_len,
    int *out_target_state
) {
    int n_states = 2 * n_edges + 1;

    for (int i = 0; i < n_states; i++) {
        out_dist[i] = HUGE_VAL;
        out_prev[i] = -1;
        out_real[i] = 0.0;
        out_heat_w[i] = 0.0;
        out_heat_len[i] = 0.0;
    }
    out_dist[0] = 0.0;
    *out_target_state = -1;
    double best_target_cost = HUGE_VAL;

    Heap *pq = heap_create(2048);
    heap_push(pq, 0.0, 0);

    int relaxed = 0;

    while (1) {
        double d; int u_state;
        if (!heap_pop(pq, &d, &u_state)) break;

        if (d > out_dist[u_state]) continue;
        if (d > max_cost) break;

        relaxed++;

        int u_in_edge = (u_state == 0) ? -1 : ((u_state - 1) >> 1);
        int u_node;
        if (u_state == 0) {
            u_node = start_node;
        } else {
            int dir = (u_state - 1) & 1;
            u_node = (dir == 0) ? edge_v[u_in_edge] : edge_u[u_in_edge];
        }

        if (target_node >= 0 && u_node == target_node && u_state != 0) {
            if (d < best_target_cost) {
                best_target_cost = d;
                *out_target_state = u_state;
                /* tighten upper bound to enable early-exit */
                if (max_cost > d) max_cost = d;
            }
        }

        int s = adj_off[u_node];
        int e = adj_off[u_node + 1];
        for (int k = s; k < e; k++) {
            int eid = adj_edge[k];
            int v_node = adj_to[k];

            if (eid == u_in_edge) continue;  /* no immediate U-turn on same edge */

            double base = edge_len[eid] * (1.0 + alpha * (1.0 - edge_heat[eid]));

            double turn_c = 0.0;
            if (u_in_edge >= 0) {
                int prev_from = (edge_u[u_in_edge] == u_node) ? edge_v[u_in_edge] : edge_u[u_in_edge];
                double ct = turn_cos_c(node_lon, node_lat, prev_from, u_node, v_node);
                if (ct >  1.0) ct =  1.0;
                if (ct < -1.0) ct = -1.0;
                double angle_deg = acos(ct) * 180.0 / M_PI;
                double over = angle_deg - 5.0;
                if (over > 0.0) turn_c = turn_pen_m * over / 85.0;
            }

            double stop_c = 0.0;
            if (u_in_edge >= 0) {
                if (signal_flag[u_node]) {
                    stop_c = signal_pen_m;
                } else {
                    int extra = node_degree[u_node] - 2;
                    if (extra > 0) stop_c = (double)extra * inter_pen_m;
                }
            }

            double mult = 1.0;
            if (penalised_edge_flag && penalised_edge_flag[eid]) mult *= 5.0;
            if (corridor_flag && corridor_mult > 1.0
                && corridor_flag[u_node] && corridor_flag[v_node]) {
                mult *= corridor_mult;
            }

            double ec = base * mult + turn_c + stop_c;
            double nd = d + ec;
            if (nd > max_cost) continue;

            int new_state;
            if (edge_v[eid] == v_node) {
                new_state = 2 * eid + 1;
            } else {
                new_state = 2 * eid + 2;
            }

            if (nd < out_dist[new_state]) {
                out_dist[new_state] = nd;
                out_prev[new_state] = u_state;
                out_real[new_state] = out_real[u_state] + edge_len[eid];
                out_heat_w[new_state] = out_heat_w[u_state] + edge_heat[eid] * edge_len[eid];
                out_heat_len[new_state] = out_heat_len[u_state] + edge_len[eid];
                heap_push(pq, nd, new_state);
            }
        }
    }

    heap_destroy(pq);
    return relaxed;
}
