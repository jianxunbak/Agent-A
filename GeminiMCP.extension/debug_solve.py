"""Debug script to identify which OR-Tools constraint causes INFEASIBLE."""
import sys
sys.path.insert(0, 'lib')
sys.path.insert(0, '.')
from ortools.sat.python import cp_model as cp

GRID = 50
def g(mm): return int(round(mm / GRID))

dx1, dy1 = 0, 0
dx2, dy2 = g(99600), g(99600)
anc_w_g = g(52825) - g(46775)   # 121
anc_d_g = g(84400) - g(75000)   # 188
sz_ax1_lo, sz_ax1_hi = g(7000), g(93550)
sz_ay1_lo, sz_ay1_hi = 0, g(90200)
clr_x1_g, clr_y1_g = g(46775), g(78200)
clr_x2_g, clr_y2_g = g(52825), g(81200)
MIN_CLR_G = g(1200)
MIN_FL_G = g(1100)
MIN_D_G = g(1000)
MIN_ANC_OV_G = g(1100)
preferred_side = 'E'

INV = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}


def build_base():
    model = cp.CpModel()
    names = ['fire_lift', 'lobby', 'staircase']
    raw = {
        'fire_lift':  (g(3200), g(3200)),
        'lobby':      (g(3200), g(3200)),
        'staircase':  (g(3450), g(7000)),
    }
    pos = {}; rot = {}; ew = {}; ed = {}
    for n in names:
        wg, dg = raw[n]
        pos[n] = {
            'x': model.NewIntVar(dx1, dx2, n + '_x'),
            'y': model.NewIntVar(dy1, dy2, n + '_y'),
        }
        rot[n] = model.NewBoolVar(n + '_rot')
        mx = max(wg, dg)
        ew[n] = model.NewIntVar(0, mx, n + '_ew')
        ed[n] = model.NewIntVar(0, mx, n + '_ed')
        model.Add(ew[n] == wg).OnlyEnforceIf(rot[n].Not())
        model.Add(ed[n] == dg).OnlyEnforceIf(rot[n].Not())
        model.Add(ew[n] == dg).OnlyEnforceIf(rot[n])
        model.Add(ed[n] == wg).OnlyEnforceIf(rot[n])

    x_ivs = []; y_ivs = []
    ex = {}; ey = {}
    for n in names:
        exv = model.NewIntVar(dx1, dx2 + 140, n + '_ex')
        eyv = model.NewIntVar(dy1, dy2 + 140, n + '_ey')
        model.Add(exv == pos[n]['x'] + ew[n])
        model.Add(eyv == pos[n]['y'] + ed[n])
        ex[n] = exv; ey[n] = eyv
        x_ivs.append(model.NewIntervalVar(pos[n]['x'], ew[n], exv, n + '_xi'))
        y_ivs.append(model.NewIntervalVar(pos[n]['y'], ed[n], eyv, n + '_yi'))

    ax = model.NewIntVar(sz_ax1_lo, sz_ax1_hi, 'ax')
    ay = model.NewIntVar(sz_ay1_lo, sz_ay1_hi, 'ay')
    ax2v = model.NewIntVar(sz_ax1_lo + anc_w_g, sz_ax1_hi + anc_w_g, 'ax2')
    ay2v = model.NewIntVar(sz_ay1_lo + anc_d_g, sz_ay1_hi + anc_d_g, 'ay2')
    model.Add(ax2v == ax + anc_w_g)
    model.Add(ay2v == ay + anc_d_g)
    x_ivs.append(model.NewIntervalVar(ax, anc_w_g, ax2v, 'anc_xi'))
    y_ivs.append(model.NewIntervalVar(ay, anc_d_g, ay2v, 'anc_yi'))

    x_ivs.append(model.NewFixedSizeIntervalVar(g(39800), g(20000), 'hole_xi'))
    y_ivs.append(model.NewFixedSizeIntervalVar(g(39800), g(20000), 'hole_yi'))
    model.AddNoOverlap2D(x_ivs, y_ivs)

    for n in names:
        model.Add(pos[n]['x'] >= dx1)
        model.Add(pos[n]['y'] >= dy1)
        model.Add(pos[n]['x'] + ew[n] <= dx2)
        model.Add(pos[n]['y'] + ed[n] <= dy2)

    return model, pos, rot, ew, ed, ex, ey, ax, ay, names


def add_touch(model, a_pos, a_ew, a_ed, b_pos, b_ew, b_ed, label):
    result = {}
    ax_, ay_ = a_pos['x'], a_pos['y']
    bx, by = b_pos['x'], b_pos['y']
    for d in ('N', 'S', 'E', 'W'):
        t = model.NewBoolVar('{}_{}'.format(label, d))
        if d == 'N':
            model.Add(ay_ + a_ed == by).OnlyEnforceIf(t)
            model.Add(ay_ + a_ed != by).OnlyEnforceIf(t.Not())
        elif d == 'S':
            model.Add(by + b_ed == ay_).OnlyEnforceIf(t)
            model.Add(by + b_ed != ay_).OnlyEnforceIf(t.Not())
        elif d == 'E':
            model.Add(ax_ + a_ew == bx).OnlyEnforceIf(t)
            model.Add(ax_ + a_ew != bx).OnlyEnforceIf(t.Not())
        else:
            model.Add(bx + b_ew == ax_).OnlyEnforceIf(t)
            model.Add(bx + b_ew != ax_).OnlyEnforceIf(t.Not())
        if d in ('N', 'S'):
            os_ = model.NewIntVar(-10**6, 10**6, '{}_{}_os'.format(label, d))
            oe_ = model.NewIntVar(-10**6, 10**6, '{}_{}_oe'.format(label, d))
            model.AddMaxEquality(os_, [ax_, bx])
            model.AddMinEquality(oe_, [ax_ + a_ew, bx + b_ew])
        else:
            os_ = model.NewIntVar(-10**6, 10**6, '{}_{}_os'.format(label, d))
            oe_ = model.NewIntVar(-10**6, 10**6, '{}_{}_oe'.format(label, d))
            model.AddMaxEquality(os_, [ay_, by])
            model.AddMinEquality(oe_, [ay_ + a_ed, by + b_ed])
        ovr = model.NewIntVar(-10**6, 10**6, '{}_{}_or'.format(label, d))
        ov = model.NewIntVar(0, 10**6, '{}_{}_ov'.format(label, d))
        model.Add(ovr == oe_ - os_)
        model.AddMaxEquality(ov, [ovr, model.NewConstant(0)])
        result[d] = (t, ov)
    return result


def req_adj(model, touch, min_ov):
    valid = []
    for d, (t, ov) in touch.items():
        uid = id(touch) % 99999
        vt = model.NewBoolVar('radj_{}_{}'.format(uid, d))
        model.AddImplication(vt, t)
        model.Add(ov >= min_ov).OnlyEnforceIf(vt)
        model.AddImplication(t.Not(), vt.Not())
        valid.append(vt)
    model.AddBoolOr(valid)


def solve(model, pos, rot, ax, ay, names, label):
    s = cp.CpSolver()
    s.parameters.max_time_in_seconds = 10
    s.parameters.num_search_workers = 1
    st = s.Solve(model)
    status = {2: 'FEASIBLE', 3: 'INFEASIBLE', 4: 'OPTIMAL'}.get(st, str(st))
    print('{}: {}'.format(label, status))
    if st in (2, 4):
        for n in names:
            print('  {}: ({},{}) rot={}'.format(
                n, s.Value(pos[n]['x']) * GRID, s.Value(pos[n]['y']) * GRID,
                s.Value(rot[n])))
        print('  anc: ({},{})'.format(s.Value(ax) * GRID, s.Value(ay) * GRID))
    return st in (2, 4), s


def full_test(add_r7=False, add_r4bx=False, label='test'):
    m, pos, rot, ew, ed, ex, ey, ax, ay, names = build_base()
    anc_pos = {'x': ax, 'y': ay}
    anc_ew_c = m.NewConstant(anc_w_g)
    anc_ed_c = m.NewConstant(anc_d_g)

    # Rule 3
    tfa = add_touch(m, anc_pos, anc_ew_c, anc_ed_c,
                    pos['fire_lift'], ew['fire_lift'], ed['fire_lift'], 'fl_anc')
    va = []
    for d, (t, ov) in tfa.items():
        vt = m.NewBoolVar('av_{}'.format(d))
        m.AddImplication(vt, t)
        m.Add(ov >= MIN_ANC_OV_G).OnlyEnforceIf(vt)
        m.AddImplication(t.Not(), vt.Not())
        va.append(vt)
    m.AddBoolOr(va)

    # Rule 1a
    tlf = add_touch(m, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'],
                    pos['lobby'], ew['lobby'], ed['lobby'], 'lb_fl')
    req_adj(m, tlf, MIN_FL_G)
    tN, _ = tlf['N']; tS, _ = tlf['S']; tE, _ = tlf['E']; tW, _ = tlf['W']
    m.Add(ew['fire_lift'] == ew['lobby']).OnlyEnforceIf(tN)
    m.Add(pos['fire_lift']['x'] == pos['lobby']['x']).OnlyEnforceIf(tN)
    m.Add(ew['fire_lift'] == ew['lobby']).OnlyEnforceIf(tS)
    m.Add(pos['fire_lift']['x'] == pos['lobby']['x']).OnlyEnforceIf(tS)
    m.Add(pos['fire_lift']['y'] == pos['lobby']['y']).OnlyEnforceIf(tE)
    m.Add(pos['fire_lift']['y'] == pos['lobby']['y']).OnlyEnforceIf(tW)

    # Rule 1b
    tls = add_touch(m, pos['lobby'], ew['lobby'], ed['lobby'],
                    pos['staircase'], ew['staircase'], ed['staircase'], 'lb_st')
    req_adj(m, tls, MIN_D_G)
    tsN, _ = tls['N']; tsS, _ = tls['S']
    m.Add(pos['lobby']['x'] == pos['staircase']['x']).OnlyEnforceIf(tsN)
    m.Add(pos['lobby']['x'] == pos['staircase']['x']).OnlyEnforceIf(tsS)

    # Rule 1c
    tfs = add_touch(m, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'],
                    pos['staircase'], ew['staircase'], ed['staircase'], 'fl_st')
    for d, (t, ov) in tfs.items():
        m.Add(t == 0)

    # Anchor touching lobby/staircase (for rules 5 and 7)
    tla = add_touch(m, anc_pos, anc_ew_c, anc_ed_c,
                    pos['lobby'], ew['lobby'], ed['lobby'], 'lb_anc')
    tsa = add_touch(m, anc_pos, anc_ew_c, anc_ed_c,
                    pos['staircase'], ew['staircase'], ed['staircase'], 'st_anc')

    # Rule 5: lobby <= 2 shared faces
    lobby_face = {'N': [], 'S': [], 'E': [], 'W': []}
    for d, (t, ov) in tlf.items():
        lobby_face[INV[d]].append(t)
    for d, (t, ov) in tls.items():
        lobby_face[d].append(t)
    for d, (t, ov) in tla.items():
        lobby_face[INV[d]].append(t)
    face_bools = []
    for face in ('N', 'S', 'E', 'W'):
        tl = lobby_face[face]
        fs = m.NewBoolVar('lbf_{}'.format(face))
        if tl:
            m.AddBoolOr(tl).OnlyEnforceIf(fs)
            for t in tl:
                m.AddImplication(t, fs)
            m.AddBoolAnd([t.Not() for t in tl]).OnlyEnforceIf(fs.Not())
        else:
            m.Add(fs == 0)
        face_bools.append(fs)
    m.Add(sum(face_bools) <= 2)

    if add_r7:
        # Rule 7: staircase has >= 1 free face
        stair_face = {'N': [], 'S': [], 'E': [], 'W': []}
        for d, (t, ov) in tls.items():
            stair_face[INV[d]].append(t)
        for d, (t, ov) in tsa.items():
            stair_face[INV[d]].append(t)
        sfb = []
        for face in ('N', 'S', 'E', 'W'):
            tl = stair_face[face]
            ff = m.NewBoolVar('stf_{}'.format(face))
            if tl:
                nt = m.NewBoolVar('stn_{}'.format(face))
                m.AddBoolAnd([t.Not() for t in tl]).OnlyEnforceIf(nt)
                m.AddBoolOr(tl).OnlyEnforceIf(nt.Not())
                m.Add(ff == nt)
            else:
                m.Add(ff == 1)
            sfb.append(ff)
        m.AddBoolOr(sfb)

    if add_r4bx:
        # Rule 4b-X: all modules clear west OR all clear east
        _wc = []; _ec = []
        for n in names:
            px = pos[n]['x']; exx = ex[n]
            wc = m.NewBoolVar('{}_wc'.format(n))
            m.Add(px >= clr_x1_g + MIN_CLR_G).OnlyEnforceIf(wc)
            m.Add(px < clr_x1_g + MIN_CLR_G).OnlyEnforceIf(wc.Not())
            _wc.append(wc)
            ec = m.NewBoolVar('{}_ec'.format(n))
            m.Add(exx <= clr_x2_g - MIN_CLR_G).OnlyEnforceIf(ec)
            m.Add(exx > clr_x2_g - MIN_CLR_G).OnlyEnforceIf(ec.Not())
            _ec.append(ec)
        awc = m.NewBoolVar('all_wc'); aec = m.NewBoolVar('all_ec')
        m.AddBoolAnd(_wc).OnlyEnforceIf(awc)
        m.AddBoolOr([b.Not() for b in _wc]).OnlyEnforceIf(awc.Not())
        m.AddBoolAnd(_ec).OnlyEnforceIf(aec)
        m.AddBoolOr([b.Not() for b in _ec]).OnlyEnforceIf(aec.Not())
        m.AddBoolOr([awc, aec])

    solve(m, pos, rot, ax, ay, names, label)


def add_side_pref(model, preferred_side, ax, ay, pos, ew, ed, names):
    """_add_side_preference with movable anchor."""
    sum_cx = model.NewIntVar(-10**8, 10**8, "sum_cx")
    sum_cy = model.NewIntVar(-10**8, 10**8, "sum_cy")
    cx_terms = []; cy_terms = []
    for name in names:
        cxi = model.NewIntVar(-10**8, 10**8, name + "_cx2")
        cyi = model.NewIntVar(-10**8, 10**8, name + "_cy2")
        model.Add(cxi * 2 == pos[name]['x'] * 2 + ew[name])
        model.Add(cyi * 2 == pos[name]['y'] * 2 + ed[name])
        cx_terms.append(cxi); cy_terms.append(cyi)
    model.Add(sum_cx == sum(cx_terms))
    model.Add(sum_cy == sum(cy_terms))
    n = len(names)
    anc_cy2 = model.NewIntVar(-10**8, 10**8, "anc_cy2")
    anc_cx2 = model.NewIntVar(-10**8, 10**8, "anc_cx2")
    model.Add(anc_cy2 == ay * 2 + anc_d_g)
    model.Add(anc_cx2 == ax * 2 + anc_w_g)
    if preferred_side == 'N':
        model.Add(sum_cy * 2 >= anc_cy2 * n)
    elif preferred_side == 'S':
        model.Add(sum_cy * 2 <= anc_cy2 * n)
    elif preferred_side == 'E':
        model.Add(sum_cx * 2 >= anc_cx2 * n)
    elif preferred_side == 'W':
        model.Add(sum_cx * 2 <= anc_cx2 * n)


print("=== Testing individual constraint combinations ===")
full_test(add_r7=False, add_r4bx=False, label='R1+R3+R5')
full_test(add_r7=True,  add_r4bx=False, label='R1+R3+R5+R7')
full_test(add_r7=False, add_r4bx=True,  label='R1+R3+R5+R4bX')
full_test(add_r7=True,  add_r4bx=True,  label='R1+R3+R5+R7+R4bX (FULL)')

print()
print("=== Testing with side preference (E) ===")

def full_test_with_pref(preferred_side, label='test'):
    m, pos, rot, ew, ed, ex, ey, ax, ay, names = build_base()
    anc_pos = {'x': ax, 'y': ay}
    anc_ew_c = m.NewConstant(anc_w_g); anc_ed_c = m.NewConstant(anc_d_g)
    tfa = add_touch(m, anc_pos, anc_ew_c, anc_ed_c, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'], 'fl_anc')
    va = []
    for d, (t, ov) in tfa.items():
        vt = m.NewBoolVar('av_{}'.format(d)); m.AddImplication(vt, t); m.Add(ov >= MIN_ANC_OV_G).OnlyEnforceIf(vt); m.AddImplication(t.Not(), vt.Not()); va.append(vt)
    m.AddBoolOr(va)
    tlf = add_touch(m, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'], pos['lobby'], ew['lobby'], ed['lobby'], 'lb_fl')
    req_adj(m, tlf, MIN_FL_G)
    tN, _ = tlf['N']; tS, _ = tlf['S']; tE, _ = tlf['E']; tW, _ = tlf['W']
    m.Add(ew['fire_lift'] == ew['lobby']).OnlyEnforceIf(tN); m.Add(pos['fire_lift']['x'] == pos['lobby']['x']).OnlyEnforceIf(tN)
    m.Add(ew['fire_lift'] == ew['lobby']).OnlyEnforceIf(tS); m.Add(pos['fire_lift']['x'] == pos['lobby']['x']).OnlyEnforceIf(tS)
    m.Add(pos['fire_lift']['y'] == pos['lobby']['y']).OnlyEnforceIf(tE); m.Add(pos['fire_lift']['y'] == pos['lobby']['y']).OnlyEnforceIf(tW)
    tls = add_touch(m, pos['lobby'], ew['lobby'], ed['lobby'], pos['staircase'], ew['staircase'], ed['staircase'], 'lb_st')
    req_adj(m, tls, MIN_D_G)
    tsN, _ = tls['N']; tsS, _ = tls['S']
    m.Add(pos['lobby']['x'] == pos['staircase']['x']).OnlyEnforceIf(tsN); m.Add(pos['lobby']['x'] == pos['staircase']['x']).OnlyEnforceIf(tsS)
    tfs = add_touch(m, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'], pos['staircase'], ew['staircase'], ed['staircase'], 'fl_st')
    for d, (t, ov) in tfs.items(): m.Add(t == 0)
    tla = add_touch(m, anc_pos, anc_ew_c, anc_ed_c, pos['lobby'], ew['lobby'], ed['lobby'], 'lb_anc')
    tsa = add_touch(m, anc_pos, anc_ew_c, anc_ed_c, pos['staircase'], ew['staircase'], ed['staircase'], 'st_anc')
    lobby_face = {'N': [], 'S': [], 'E': [], 'W': []}
    for d, (t, ov) in tlf.items(): lobby_face[INV[d]].append(t)
    for d, (t, ov) in tls.items(): lobby_face[d].append(t)
    for d, (t, ov) in tla.items(): lobby_face[INV[d]].append(t)
    face_bools = []
    for face in ('N', 'S', 'E', 'W'):
        tl = lobby_face[face]; fs = m.NewBoolVar('lbf_{}'.format(face))
        if tl:
            m.AddBoolOr(tl).OnlyEnforceIf(fs)
            for t in tl: m.AddImplication(t, fs)
            m.AddBoolAnd([t.Not() for t in tl]).OnlyEnforceIf(fs.Not())
        else: m.Add(fs == 0)
        face_bools.append(fs)
    m.Add(sum(face_bools) <= 2)
    stair_face = {'N': [], 'S': [], 'E': [], 'W': []}
    for d, (t, ov) in tls.items(): stair_face[INV[d]].append(t)
    for d, (t, ov) in tsa.items(): stair_face[INV[d]].append(t)
    sfb = []
    for face in ('N', 'S', 'E', 'W'):
        tl = stair_face[face]; ff = m.NewBoolVar('stf_{}'.format(face))
        if tl:
            nt = m.NewBoolVar('stn_{}'.format(face)); m.AddBoolAnd([t.Not() for t in tl]).OnlyEnforceIf(nt); m.AddBoolOr(tl).OnlyEnforceIf(nt.Not()); m.Add(ff == nt)
        else: m.Add(ff == 1)
        sfb.append(ff)
    m.AddBoolOr(sfb)
    # Rule 4b-X
    _wc = []; _ec = []
    for n in names:
        px = pos[n]['x']; exx = ex[n]
        wc = m.NewBoolVar('{}_wc'.format(n)); m.Add(px >= clr_x1_g + MIN_CLR_G).OnlyEnforceIf(wc); m.Add(px < clr_x1_g + MIN_CLR_G).OnlyEnforceIf(wc.Not()); _wc.append(wc)
        ec = m.NewBoolVar('{}_ec'.format(n)); m.Add(exx <= clr_x2_g - MIN_CLR_G).OnlyEnforceIf(ec); m.Add(exx > clr_x2_g - MIN_CLR_G).OnlyEnforceIf(ec.Not()); _ec.append(ec)
    awc = m.NewBoolVar('all_wc'); aec = m.NewBoolVar('all_ec')
    m.AddBoolAnd(_wc).OnlyEnforceIf(awc); m.AddBoolOr([b.Not() for b in _wc]).OnlyEnforceIf(awc.Not())
    m.AddBoolAnd(_ec).OnlyEnforceIf(aec); m.AddBoolOr([b.Not() for b in _ec]).OnlyEnforceIf(aec.Not())
    m.AddBoolOr([awc, aec])
    # Side preference
    add_side_pref(m, preferred_side, ax, ay, pos, ew, ed, names)
    solve(m, pos, rot, ax, ay, names, label)

full_test_with_pref('E', 'FULL + side_pref=E')
full_test_with_pref('W', 'FULL + side_pref=W')

print()
print("=== Bisecting: adding side pref to smaller subsets ===")

def partial_test(add_r5=False, add_r7=False, add_r4bx=False, preferred_side='E', label='test'):
    m, pos, rot, ew, ed, ex, ey, ax, ay, names = build_base()
    anc_pos = {'x': ax, 'y': ay}
    anc_ew_c = m.NewConstant(anc_w_g); anc_ed_c = m.NewConstant(anc_d_g)
    tfa = add_touch(m, anc_pos, anc_ew_c, anc_ed_c, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'], 'fl_anc')
    va = []
    for d, (t, ov) in tfa.items():
        vt = m.NewBoolVar('av_{}'.format(d)); m.AddImplication(vt, t); m.Add(ov >= MIN_ANC_OV_G).OnlyEnforceIf(vt); m.AddImplication(t.Not(), vt.Not()); va.append(vt)
    m.AddBoolOr(va)
    tlf = add_touch(m, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'], pos['lobby'], ew['lobby'], ed['lobby'], 'lb_fl')
    req_adj(m, tlf, MIN_FL_G)
    tN, _ = tlf['N']; tS, _ = tlf['S']; tE, _ = tlf['E']; tW, _ = tlf['W']
    m.Add(ew['fire_lift'] == ew['lobby']).OnlyEnforceIf(tN); m.Add(pos['fire_lift']['x'] == pos['lobby']['x']).OnlyEnforceIf(tN)
    m.Add(ew['fire_lift'] == ew['lobby']).OnlyEnforceIf(tS); m.Add(pos['fire_lift']['x'] == pos['lobby']['x']).OnlyEnforceIf(tS)
    m.Add(pos['fire_lift']['y'] == pos['lobby']['y']).OnlyEnforceIf(tE); m.Add(pos['fire_lift']['y'] == pos['lobby']['y']).OnlyEnforceIf(tW)
    tls = add_touch(m, pos['lobby'], ew['lobby'], ed['lobby'], pos['staircase'], ew['staircase'], ed['staircase'], 'lb_st')
    req_adj(m, tls, MIN_D_G)
    tsN, _ = tls['N']; tsS, _ = tls['S']
    m.Add(pos['lobby']['x'] == pos['staircase']['x']).OnlyEnforceIf(tsN); m.Add(pos['lobby']['x'] == pos['staircase']['x']).OnlyEnforceIf(tsS)
    tfs = add_touch(m, pos['fire_lift'], ew['fire_lift'], ed['fire_lift'], pos['staircase'], ew['staircase'], ed['staircase'], 'fl_st')
    for d, (t, ov) in tfs.items(): m.Add(t == 0)
    tla = add_touch(m, anc_pos, anc_ew_c, anc_ed_c, pos['lobby'], ew['lobby'], ed['lobby'], 'lb_anc')
    tsa = add_touch(m, anc_pos, anc_ew_c, anc_ed_c, pos['staircase'], ew['staircase'], ed['staircase'], 'st_anc')
    if add_r5:
        lobby_face = {'N': [], 'S': [], 'E': [], 'W': []}
        for d, (t, ov) in tlf.items(): lobby_face[INV[d]].append(t)
        for d, (t, ov) in tls.items(): lobby_face[d].append(t)
        for d, (t, ov) in tla.items(): lobby_face[INV[d]].append(t)
        face_bools = []
        for face in ('N', 'S', 'E', 'W'):
            tl = lobby_face[face]; fs = m.NewBoolVar('lbf_{}'.format(face))
            if tl:
                m.AddBoolOr(tl).OnlyEnforceIf(fs)
                for t in tl: m.AddImplication(t, fs)
                m.AddBoolAnd([t.Not() for t in tl]).OnlyEnforceIf(fs.Not())
            else: m.Add(fs == 0)
            face_bools.append(fs)
        m.Add(sum(face_bools) <= 2)
    if add_r7:
        stair_face = {'N': [], 'S': [], 'E': [], 'W': []}
        for d, (t, ov) in tls.items(): stair_face[INV[d]].append(t)
        for d, (t, ov) in tsa.items(): stair_face[INV[d]].append(t)
        sfb = []
        for face in ('N', 'S', 'E', 'W'):
            tl = stair_face[face]; ff = m.NewBoolVar('stf_{}'.format(face))
            if tl:
                nt = m.NewBoolVar('stn_{}'.format(face)); m.AddBoolAnd([t.Not() for t in tl]).OnlyEnforceIf(nt); m.AddBoolOr(tl).OnlyEnforceIf(nt.Not()); m.Add(ff == nt)
            else: m.Add(ff == 1)
            sfb.append(ff)
        m.AddBoolOr(sfb)
    if add_r4bx:
        _wc = []; _ec = []
        for n in names:
            px = pos[n]['x']; exx = ex[n]
            wc = m.NewBoolVar('{}_wc'.format(n)); m.Add(px >= clr_x1_g + MIN_CLR_G).OnlyEnforceIf(wc); m.Add(px < clr_x1_g + MIN_CLR_G).OnlyEnforceIf(wc.Not()); _wc.append(wc)
            ec = m.NewBoolVar('{}_ec'.format(n)); m.Add(exx <= clr_x2_g - MIN_CLR_G).OnlyEnforceIf(ec); m.Add(exx > clr_x2_g - MIN_CLR_G).OnlyEnforceIf(ec.Not()); _ec.append(ec)
        awc = m.NewBoolVar('all_wc'); aec = m.NewBoolVar('all_ec')
        m.AddBoolAnd(_wc).OnlyEnforceIf(awc); m.AddBoolOr([b.Not() for b in _wc]).OnlyEnforceIf(awc.Not())
        m.AddBoolAnd(_ec).OnlyEnforceIf(aec); m.AddBoolOr([b.Not() for b in _ec]).OnlyEnforceIf(aec.Not())
        m.AddBoolOr([awc, aec])
    add_side_pref(m, preferred_side, ax, ay, pos, ew, ed, names)
    solve(m, pos, rot, ax, ay, names, label)

partial_test(add_r5=False, add_r7=False, add_r4bx=False, preferred_side='E', label='R1+R3 + pref_E')
partial_test(add_r5=True,  add_r7=False, add_r4bx=False, preferred_side='E', label='R1+R3+R5 + pref_E')
partial_test(add_r5=False, add_r7=True,  add_r4bx=False, preferred_side='E', label='R1+R3+R7 + pref_E')
partial_test(add_r5=False, add_r7=False, add_r4bx=True,  preferred_side='E', label='R1+R3+R4bX + pref_E')
partial_test(add_r5=True,  add_r7=True,  add_r4bx=False, preferred_side='E', label='R1+R3+R5+R7 + pref_E')
partial_test(add_r5=True,  add_r7=False, add_r4bx=True,  preferred_side='E', label='R1+R3+R5+R4bX + pref_E')
partial_test(add_r5=False, add_r7=True,  add_r4bx=True,  preferred_side='E', label='R1+R3+R7+R4bX + pref_E')
