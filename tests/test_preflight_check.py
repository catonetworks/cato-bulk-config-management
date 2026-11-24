"""Offline tests for scripts/preflight_check.py (no API calls).

Run: python3 -m unittest discover -s tests -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import preflight_check as pc  # noqa: E402


def rule(family, name, **raw):
    return {"family": family, "name": name, "raw": dict(raw, name=name)}


def export(rules=(), sites=None, allocated=(), ranges=(), groups=(), webhooks=()):
    return {"dir": "x", "rules": list(rules), "policy_enabled": {}, "missing_files": [], "sites": sites or {},
            "global_ip_ranges": list(ranges), "allocated_ips": list(allocated), "static_hosts": [],
            "webhooks": list(webhooks), "groups": list(groups), "custom_apps": []}


def tenant(**kw):
    t = {"account_id": "1", "account": {"name": "t", "plan": "COMMERCIAL"}, "licenses": [], "allocations": {},
         "allocated_ips": [], "locations": set(), "sites": set(), "host_total": 0, "users": [], "user_groups": [],
         "groups": set(), "policies": {}}
    t.update(kw)
    return t


class FakeApi:
    scim_url = None
    _scim_token = None


class CollectRefs(unittest.TestCase):
    def test_nested_refs_are_labelled_by_rule(self):
        r = rule("internetFirewall", "R1", source={"usersGroup": [{"id": "1", "name": "Admins"}]},
                 destination={"customCategory": [{"name": "Pen Testing"}]}, device=[{"name": "Mac posture"}])
        refs = pc.collect_refs([r])
        self.assertEqual(refs["usersGroup"]["Admins"], ["internetFirewall: R1"])
        self.assertIn("Pen Testing", refs["customCategory"])
        self.assertIn("Mac posture", refs["device"])

    def test_single_object_refs(self):
        r = rule("wanNetwork", "W1", bandwidthPriority={"name": "50"})
        self.assertEqual(pc.collect_refs([r])["bandwidthPriority"], {"50": ["wanNetwork: W1"]})


class EntityState(unittest.TestCase):
    def test_success_is_present(self):
        self.assertEqual(pc.Prober.entity_state(("SUCCESS", []), "X")[0], "PRESENT")

    def test_invalid_entity_naming_the_object_is_missing(self):
        err = [{"errorCode": "ruleHasInvalidEntity", "errorMessage": "Rule has an invalid entity: CustomCategory(name: X)"}]
        self.assertEqual(pc.Prober.entity_state(("FAILURE", err), "X")[0], "MISSING")

    def test_invalid_entity_for_something_else_is_error(self):
        err = [{"errorCode": "ruleHasInvalidEntity", "errorMessage": "Rule has an invalid entity: Application(name: Y)"}]
        self.assertEqual(pc.Prober.entity_state(("FAILURE", err), "X")[0], "ERROR")

    def test_other_validation_errors_mean_the_entity_resolved(self):
        err = [{"errorCode": "applicationControl-400", "errorMessage": "action: MONITOR action is not allowed"}]
        self.assertEqual(pc.Prober.entity_state(("FAILURE", err), "X")[0], "PRESENT")


class Checks(unittest.TestCase):
    def test_cma_only_without_probe_is_action_and_with_probe_is_blocker(self):
        refs = pc.collect_refs([rule("wanNetwork", "W1", bandwidthPriority={"name": "50"}),
                                rule("wanNetwork", "W2", bandwidthPriority={"name": "10"})])
        rep = pc.Report()
        pc.check_cma_only(rep, refs, None)
        self.assertEqual([f["severity"] for f in rep.findings], ["ACTION"])  # 10 is a default priority
        rep = pc.Report()
        pc.check_cma_only(rep, refs, {"entities": {("bandwidthPriority", "50"): ("MISSING", "")}})
        self.assertEqual(rep.findings[0]["severity"], "BLOCKER")
        self.assertEqual(rep.findings[0]["rules"], ["wanNetwork: W1"])

    def test_link_local_range_and_real_translation(self):
        sites = {"S1": {"name": "S1", "_kind": "socket", "native_range": {"subnet": "10.0.0.0/24", "translated_subnet": "10.9.0.0/24"},
                        "lan_interfaces": [], "wifi_ssids": []}}
        exp = export(sites=sites, ranges=[{"name": "Meta", "ipRange": "169.254.169.254/32"}])
        rep = pc.Report()
        pc.check_features(rep, exp, {}, None, deps_deployed=False)
        sev = {f["title"].split(" ")[0]: f["severity"] for f in rep.findings}
        self.assertEqual(sev.get("Global"), "BLOCKER")
        self.assertTrue(any(f["severity"] == "ACTION" and "Static Range Translation" in f["title"] for f in rep.findings))

    def test_lan_layer7_names_the_sites(self):
        nr = {"name": "NR", "site": {"site": [{"name": "Branch-1"}], "group": []}}
        fw = {"family": "socketLan", "name": "NR :: FW", "raw": {"name": "FW", "application": {"customApp": [{"name": "App"}]}},
              "lan_firewall": True, "network_rule": nr}
        rep = pc.Report()
        pc.check_features(rep, export(rules=[fw]), {}, None)
        l7 = [f for f in rep.findings if "Layer 7" in f["title"]][0]
        self.assertIn("Branch-1", l7["items"][0])
        self.assertEqual(l7["rules"], ["socketLan: NR :: FW"])

    def test_missing_pop_blocks_dependant_rules(self):
        exp = export(allocated=[{"name": "Shanghai_DC2 - 1.2.3.4", "popLocation": {"name": "Shanghai_DC2"}}],
                     rules=[rule("wanNetwork", "Egress", configuration={"allocationIp": [{"name": "Shanghai_DC2 - 1.2.3.4"}]})])
        src = tenant(allocated_ips=[{"ip": "1.2.3.4", "popLocation": {"name": "Shanghai_DC2"}}])
        tgt = tenant(locations={"Ashburn"})
        rep = pc.Report()
        pc.check_pops(rep, exp, src, tgt, pc.collect_refs(exp["rules"]))
        blocker = [f for f in rep.findings if f["severity"] == "BLOCKER"][0]
        self.assertEqual(blocker["rules"], ["wanNetwork: Egress"])

    def test_identity_split_by_import_type(self):
        rules = [rule("internetFirewall", "R1", source={"usersGroup": [{"name": "Admins"}, {"name": "Remote"}]})]
        src = tenant(user_groups=[{"name": "Admins", "importType": "MANUAL"}, {"name": "Remote", "importType": "SCIM"}],
                     users=[{"displayName": "A", "userImportType": "SCIM"}])
        tgt = tenant(user_groups=[{"name": "Remote", "importType": "SCIM"}])
        rep = pc.Report()
        pc.check_identity(rep, export(rules=rules), src, tgt, pc.collect_refs(rules), FakeApi())
        blockers = [f for f in rep.findings if f["severity"] == "BLOCKER"]
        self.assertEqual(len(blockers), 1)
        self.assertIn("MANUAL", blockers[0]["title"])
        self.assertEqual((blockers[0]["ctx"]["groups"], blockers[0]["ctx"]["users"]), (1, 0))
        self.assertTrue(any(f["severity"] == "ACTION" and "SCIM" in f["title"] for f in rep.findings))

    def test_classic_groups_already_converted_are_ok(self):
        rules = [rule("internetFirewall", "R1", source={"group": [{"name": "Legacy"}, {"name": "Other"}]})]
        exp = export(rules=rules, groups=[{"name": "Legacy"}])
        rep = pc.Report()
        pc.check_integrity(rep, exp, tenant(groups=set()), pc.collect_refs(rules))
        sev = sorted(f["severity"] for f in rep.findings if "classic" in f["title"])
        self.assertEqual(sev, ["ACTION", "OK"])


def _read(path):
    with open(path) as fh:
        return fh.read()


class LeaveOut(unittest.TestCase):
    """The check writes the scope command and exclusion files for whatever is still missing."""

    def _report(self):
        refs = pc.collect_refs([rule("wanNetwork", "W1", bandwidthPriority={"name": "50"})])
        rep = pc.Report()
        pc.check_cma_only(rep, refs, {"entities": {("bandwidthPriority", "50"): ("MISSING", "")}})
        nr = {"name": "NR", "site": {"site": [{"name": "Branch-1"}], "group": []}}
        fw = {"family": "socketLan", "name": "NR :: FW", "raw": {"name": "FW", "application": {"customApp": [{"name": "A"}]}},
              "lan_firewall": True, "network_rule": nr}
        pc.check_features(rep, export(rules=[fw], ranges=[{"name": "AzureVDI-2", "ipRange": "169.254.169.254/32"}]), {}, None,
                          deps_deployed=False)
        rep.leave_out.add(("siteNetworkSubnet", "Site \\ LAN \\ It's a range"))  # backslashes and a quote
        return rep

    def test_findings_feed_the_leave_out_lists(self):
        rep = self._report()
        self.assertIn(("bandwidthPriority", "50"), rep.leave_out)
        self.assertIn(("globalIpRange", "AzureVDI-2"), rep.leave_out)
        self.assertEqual(rep.skip_rules, {"NR / FW"})  # nested LAN rules use the scope script's " / " form
        self.assertEqual(rep.exclude["global_ip_ranges_exclude"], {"AzureVDI-2"})

    def test_written_command_round_trips_every_name(self):
        import shlex
        import subprocess
        import tempfile
        rep = self._report()
        with tempfile.TemporaryDirectory() as d:
            written = pc.write_leave_out(rep, os.path.join(d, "config_data"), d)
            self.assertEqual(len(written), 3)
            cmd = _read(os.path.join(d, "scope_command.sh"))
            self.assertEqual(subprocess.run(["sh", "-n", os.path.join(d, "scope_command.sh")]).returncode, 0)
            argv = shlex.split(" ".join(l.rstrip("\\") for l in cmd.splitlines() if not l.startswith("#")))
            self.assertIn("siteNetworkSubnet=Site \\ LAN \\ It's a range", argv)
            self.assertIn("bandwidthPriority=50", argv)
            self.assertEqual(argv[argv.index("--skip-rule") + 1], "NR / FW")
            self.assertEqual(argv[argv.index("--out") + 1], os.path.join(d, "config_data_target"))
            tfv = _read(os.path.join(d, "exclude-0-dependencies.auto.tfvars"))
            self.assertIn('global_ip_ranges_exclude = ["AzureVDI-2"]', tfv)
            self.assertIn("allocated_ips_exclude = []", tfv)
            self.assertNotIn("wifi", _read(os.path.join(d, "exclude-1-sites.auto.tfvars")))

    def test_blocked_range_follows_the_first_deploy(self):
        # 0-dependencies' first deploy sends every range in one request; afterwards it owns every range,
        # so a hand-made range must be excluded before that deploy and must NOT be excluded after it.
        exp = export(ranges=[{"name": "AzureVDI-2", "ipRange": "169.254.169.254/32"}])
        cases = {  # (exists in target, 0-dependencies deployed) -> (severity, excluded, rules left out)
            (False, False): ("BLOCKER", True, True),
            (False, True): ("BLOCKER", True, True),
            (True, False): ("ACTION", True, False),
            (True, True): ("OK", False, False),
        }
        for (exists, deployed), (sev, excluded, left_out) in cases.items():
            rep = pc.Report()
            tgt = tenant(global_ranges={"AzureVDI-2"} if exists else set())
            pc.check_features(rep, exp, {}, None, tgt, deps_deployed=deployed)
            f = [x for x in rep.findings if x["key"].startswith("feat_waf_range")][0]
            self.assertEqual(f["severity"], sev, (exists, deployed))
            self.assertEqual("AzureVDI-2" in rep.exclude["global_ip_ranges_exclude"], excluded, (exists, deployed))
            self.assertEqual(("globalIpRange", "AzureVDI-2") in rep.leave_out, left_out, (exists, deployed))

    def test_lan_l7_confirmed_keeps_the_rules_in(self):
        nr = {"name": "NR", "site": {"site": [{"name": "Branch-1"}], "group": []}}
        fw = {"family": "socketLan", "name": "NR :: FW", "raw": {"name": "FW", "application": {"customApp": [{"name": "A"}]}},
              "lan_firewall": True, "network_rule": nr}
        rep = pc.Report()
        pc.check_features(rep, export(rules=[fw]), {}, None, deps_deployed=False, lan_l7_confirmed=True)
        self.assertEqual(rep.skip_rules, set())
        f = [x for x in rep.findings if x["key"] == "feat_l7_confirmed"][0]
        self.assertEqual(f["severity"], "OK")
        import preflight_report as pr
        self.assertIn("Branch-1", pr.ready_text(f))

    def test_nothing_missing_still_writes_a_plain_copy_command(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pc.write_leave_out(pc.Report(), os.path.join(d, "config_data"), d)
            cmd = [l for l in _read(os.path.join(d, "scope_command.sh")).splitlines() if not l.startswith("#")]
            self.assertEqual(len(cmd), 1)
            self.assertFalse(cmd[0].endswith("\\"))


class GenericAccounts(unittest.TestCase):
    """Things the reference tenant never used but other accounts will."""

    def test_aliased_references_count_as_their_object(self):
        import scope_policies_to_target as sp
        rules = [rule("ztnaAlwaysOn", "AO", devicePostureProfile=[{"name": "Laptop"}]),
                 rule("terminalServer", "TS", allowedHostIP=[{"name": "Jump"}], excludeTraffic=[{"name": "Meta"}])]
        refs = pc.collect_refs(rules)
        self.assertEqual(refs["device"], {"Laptop": ["ztnaAlwaysOn: AO"]})
        self.assertEqual(set(refs["globalIpRange"]), {"Jump", "Meta"})
        self.assertEqual(sp.blockers(rules[0]["raw"], {"device": {"Laptop"}}, False), ["device:Laptop"])
        self.assertEqual(sp.blockers(rules[1]["raw"], {"globalIpRange": {"Meta"}}, False), ["globalIpRange:Meta"])

    def test_site_licenses_counted_against_licensed_sites(self):
        sites = {n: {"name": n, "_kind": "socket"} for n in ("A", "B", "C")}
        src = tenant(licenses=[{"sku": "CATO_SITE", "status": "ACTIVE", "site": {"name": "A"}},
                               {"sku": "CATO_SITE", "status": "ACTIVE", "site": {"name": "B"}},
                               {"sku": "CATO_PB", "status": "ACTIVE", "total": 500, "allocatedBandwidth": 0,
                                "sites": [{"site": {"name": "C"}, "allocatedBandwidth": 100}]}])
        short = tenant(licenses=[{"sku": "CATO_SITE", "status": "ACTIVE", "site": None}])
        enough = tenant(licenses=[{"sku": "CATO_SITE", "status": "ACTIVE", "site": None}] * 2
                        + [{"sku": "CATO_PB", "status": "ACTIVE", "total": 200, "allocatedBandwidth": 50, "sites": []}])
        for tgt, key in ((short, "lic_sites_short"), (enough, "lic_sites_ok"), (tenant(), "lic_sites_unknown")):
            rep = pc.Report()
            pc.check_licensing(rep, export(sites=sites), src, tgt)
            f = [x for x in rep.findings if x["key"].startswith("lic_sites")][0]
            self.assertEqual(f["key"], key)
            self.assertEqual((f["ctx"]["ded"], f["ctx"]["pool"], f["ctx"]["pool_bw"]), (2, 1, 100))

    def test_sanctioned_apps_and_unchecked_objects(self):
        rules = [rule("internetFirewall", "Allow sanctioned", destination={"sanctionedAppsCategory": [{"name": "Sanctioned Apps"}]}),
                 rule("applicationControl", "Float", source={"floatingSubnet": [{"name": "Roaming"}]})]
        refs = pc.collect_refs(rules)
        rep = pc.Report()
        pc.check_features(rep, export(rules=rules), refs, None, deps_deployed=False)
        pc.check_integrity(rep, export(rules=rules), tenant(), refs)
        keys = {f["key"]: f for f in rep.findings}
        self.assertEqual(keys["feat_sanctioned"]["rules"], ["internetFirewall: Allow sanctioned"])
        self.assertEqual(keys["int_unchecked"]["severity"], "WARN")
        self.assertIn("floating subnet 'Roaming'", keys["int_unchecked"]["items"][0])


class KeyPermissions(unittest.TestCase):
    """A key whose role can't read every policy: the export stops at the first refusal, the deploy can't manage it."""

    DENIED = {"error": "Exception while fetching data (/policy/socketBypass) : Access denied: Insufficient privileges "
                       "for account 1234567"}

    def test_source_key_missing_a_permission_is_named(self):
        import preflight_report as pr
        src = tenant(policies={"internetFirewall": {"enabled": True, "rules": ["A"]}, "socketBypass": self.DENIED})
        tgt = tenant(policies={"internetFirewall": {"enabled": False, "rules": []}})
        exp = export(rules=[rule("internetFirewall", "A")])
        exp["policy_enabled"] = {"internetFirewall": True}
        rep = pc.Report()
        pc.check_permissions(rep, exp, src, tgt)
        f = [x for x in rep.findings if x["key"] == "perm_source"][0]
        self.assertEqual(f["severity"], "BLOCKER")
        self.assertEqual(f["items"], ["socketBypass: needs the 'Socket Bypass' permission"])
        g = pr.guide(f)
        self.assertEqual(g["owner"], "cma_admin")
        self.assertIn("Socket Bypass", g["why"])
        self.assertIn("Socket Bypass.", g["steps"][0])
        self.assertIn("carried on without it", g["why"])  # export_source.py doesn't stop there
        self.assertNotIn("stops", g["why"] + f["fix"].replace("on its own stops there", ""))
        self.assertNotIn("perm_ok", [x["key"] for x in rep.findings])
        pc.check_export(rep, exp, src)  # the rest of the export is complete, and says so without claiming it all is
        ok = [x for x in rep.findings if x["key"] == "export_ok"][0]
        self.assertTrue(pr.ready_text(ok).startswith("Apart from what the API key wasn't allowed to read"))
        rep2 = pc.Report()
        pc.check_export(rep2, exp, tenant(policies={"internetFirewall": {"enabled": True, "rules": ["A"]}}))
        self.assertTrue(pr.ready_text(rep2.findings[0]).startswith("Your configuration export is complete"))

    def test_policy_left_out_of_the_export_is_a_blocker(self):
        src = tenant(policies={"internetFirewall": {"enabled": True, "rules": ["A"]},
                               "wanFirewall": {"enabled": True, "rules": ["B", "C"]}})
        exp = export(rules=[rule("internetFirewall", "A")])
        exp["policy_enabled"] = {"internetFirewall": True}
        rep = pc.Report()
        pc.check_permissions(rep, exp, src, tenant(policies={"wanFirewall": {"enabled": False, "rules": []}}))
        f = [x for x in rep.findings if x["key"] == "export_policy_missing"][0]
        self.assertEqual(f["items"], ["wanFirewall: 2 rules"])
        self.assertIn("perm_ok", [x["key"] for x in rep.findings])

    def test_target_key_denied_is_one_blocker_not_a_warning_too(self):
        tgt = tenant(policies={"socketBypass": self.DENIED})
        rep = pc.Report()
        pc.check_permissions(rep, export(), tenant(policies={}), tgt)
        pc.check_target_state(rep, export(), tgt)
        keys = [x["key"] for x in rep.findings]
        self.assertIn("perm_target", keys)
        self.assertNotIn("tgt_unreadable", keys)


class ScimEndpoint(unittest.TestCase):
    """The SCIM check against a local stand-in for the Cato SCIM app."""

    def _serve(self, status):
        import http.server
        import json as _json
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                ok = self.headers.get("Authorization") == "Bearer good-token" and status == 200
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/scim+json")
                self.end_headers()
                self.wfile.write(_json.dumps({"totalResults": 7 if ok else 0}).encode())

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/scim/v2/1/abc"

    def _run(self, url, token):
        api = pc.Api.__new__(pc.Api)
        api.scim_url, api._scim_token = url, token
        rep = pc.Report()
        pc.check_identity(rep, export(), tenant(), tenant(), {}, api)
        return [f for f in rep.findings if "SCIM endpoint" in f["title"]][0]

    def test_answering_endpoint_is_ok(self):
        f = self._run(self._serve(200), "good-token")
        self.assertEqual(f["severity"], "OK")
        self.assertIn("7 total", f["title"])

    def test_rejected_token_is_blocker(self):
        self.assertEqual(self._run(self._serve(200), "bad-token")["severity"], "BLOCKER")


class CustomerReport(unittest.TestCase):
    """The Cato-branded report written for the customer."""

    def setUp(self):
        import preflight_report as pr
        self.pr = pr

    def test_every_actionable_key_has_customer_wording(self):
        import re
        src = open(pc.__file__).read()
        keys = set(re.findall(r'rep\.add\((?:cat|"access"), "(?:BLOCKER|ACTION|WARN)", "([a-z0-9_]+)"', src))
        self.assertGreater(len(keys), 30)
        ctx = {"n": 2, "need": 19, "total": 3, "have": 15, "type": "SCIM", "names": ["Shanghai_DC2"], "kind": "device",
               "one": "device posture profile", "many": "device posture profiles", "sites": ["Branch-1"], "label": "sites",
               "family": "internetFirewall"}
        owners = {o[0] for o in self.pr.OWNERS}
        generic = {"probe_failed", "probe_baseline", "tgt_unreadable", "tgt_rules_existing"}
        for k in sorted(keys - generic):
            g = self.pr.guide({"key": k, "ctx": ctx, "rules": ["a: b"], "title": "ENGINEERING TITLE", "detail": ""})
            self.assertIn(g["owner"], owners, k)
            self.assertNotEqual(g["title"], "ENGINEERING TITLE", f"{k} has no customer wording")
            self.assertTrue(g["why"], k)

    def test_identity_owner_depends_on_import_type(self):
        f = {"key": "id_missing", "rules": [], "title": "", "detail": ""}
        self.assertEqual(self.pr.guide(dict(f, ctx={"type": "SCIM", "n": 3}))["owner"], "identity")
        self.assertEqual(self.pr.guide(dict(f, ctx={"type": "MANUAL", "n": 3}))["owner"], "cma_admin")

    def test_manual_users_and_groups_are_worded_apart(self):
        # The API can create users (user.createUser) but not user groups, so the wording must not lump them.
        f = {"key": "id_missing", "rules": [], "title": "", "detail": ""}
        groups = self.pr.guide(dict(f, ctx={"type": "MANUAL", "n": 2, "groups": 2, "users": 0}))
        self.assertEqual(groups["title"], "Create 2 user groups in the CMA")
        mixed = self.pr.guide(dict(f, ctx={"type": "MANUAL", "n": 3, "groups": 1, "users": 2}))
        self.assertEqual(mixed["title"], "Create 1 user group and 2 users")
        self.assertIn("through the Cato API", mixed["why"])
        users = self.pr.guide(dict(f, ctx={"type": "MANUAL", "n": 1, "groups": 0, "users": 1}))
        self.assertEqual(users["title"], "Create 1 user")
        self.assertNotIn("can't create user groups", users["why"])

    def test_friendly_text(self):
        fr = self.pr._friendly
        self.assertEqual(fr("internetFirewall: RBI Check Apps"), "Internet Firewall: RBI Check Apps")
        self.assertEqual(fr("user group 'X' (1 rule(s))"), "user group 'X' (1 rule)")
        self.assertEqual(fr("CATO_RBI (Remote Browser Isolation)", "lic_skus_unknown"), "Remote Browser Isolation (CATO_RBI)")

    def test_render_has_brand_and_plan(self):
        rep = pc.Report()
        rep.add("pops", "BLOCKER", "pop_missing", {"n": 1, "names": ["Shanghai_DC2"]}, "x",
                items=["Shanghai_DC2: used by 1 item"], rules=["wanNetwork: Egress"])
        rep.add("features", "ACTION", "feat_wifi", {"n": 1}, "y", items=["S1:Guest"])
        rep.add("export", "OK", "export_ok", {"rules": 2, "policies": 1}, "z")
        exp = export(rules=[rule("wanNetwork", "Egress"), rule("wanNetwork", "Other")])
        meta = {"source": "Src (1)", "target": "Tgt (2)", "target_name": "Tgt", "when": "2026-11-24", "probe": True}
        out = self.pr.render_customer_html(rep, exp, meta, customer="Example Co")
        for needle in ("Migration readiness report", "Prepared for", "Example Co", "Your action plan",
                       "Required before migration", "Already in place", "Appendix A", "@font-face",
                       'class="cato-logo"', "<b>WAN Network</b>", "<span>Egress</span>",
                       ">1</span>Make 1 PoP location available"):
            self.assertTrue(needle in out, f"missing from report: {needle}")
        self.assertTrue("50%" in out, "readiness gauge should read 50%")  # 1 of 2 rules ready

    RUN = {"source_profile": "src-ro", "target_profile": "new acct", "source_account": "111", "target_account": "222",
           "config_dir": "config_data", "target_config_dir": "config_data_target", "out_dir": "preflight", "probe": True,
           "customer": None, "lan_l7_confirmed": False, "provider_version": "2.0.0", "mirror": False}

    def _meta(self, **run):
        return {"source": "Src (111)", "target": "Tgt (222)", "target_name": "Tgt", "when": "2026-11-24", "probe": True,
                "run": dict(self.RUN, **run)}

    def test_cover_and_appendix_count_the_same_rules(self):
        rep = pc.Report()
        rep.add("pops", "BLOCKER", "pop_missing", {"n": 1, "names": ["P"]}, "x", items=["P"], rules=["wanNetwork: A"])
        rep.add("integrity", "WARN", "int_stale", {"n": 1}, "y", items=["site 'Old'"], rules=["wanNetwork: B"])
        exp = export(rules=[rule("wanNetwork", n) for n in "ABCD"])
        out = self.pr.render_customer_html(rep, exp, self._meta())
        self.assertIn("2 of your 4 policy rules can move", out)
        self.assertIn("2 of your 4 policy rules wait on at least one item", out)
        self.assertIn("50%", out)

    def test_singular_wording(self):
        f = {"rules": ["wanNetwork: A"], "title": "", "detail": "", "items": []}
        g = self.pr.guide(dict(f, key="id_missing", ctx={"type": "LDAP", "n": 1, "groups": 1, "users": 0}))
        self.assertEqual(g["title"], "Sync 1 user group from your directory")
        self.assertIn("1 of your policy rules depends on this", g["why"])
        g = self.pr.guide(dict(f, key="id_missing", ctx={"type": "DYNAMIC", "n": 1, "groups": 1, "users": 0}))
        self.assertEqual(g["title"], "Turn on 1 dynamic user group")
        g = self.pr.guide(dict(f, key="id_missing", ctx={"type": "NOT IN SOURCE", "n": 1, "groups": 0, "users": 1}))
        self.assertEqual(g["title"], "Review 1 user that no longer exists")
        g = self.pr.guide(dict(f, key="lic_skus_missing", ctx={"n": 1}))
        self.assertTrue(g["why"].startswith("This service is"))
        g = self.pr.guide(dict(f, key="pop_missing", ctx={"n": 1, "names": ["P"]}))
        self.assertIn("leave its items out", g["title"])

    def test_undecided_object_hides_the_api_error(self):
        f = {"key": "cma_undecided", "rules": [], "title": "", "detail": "",
             "ctx": {"kind": "edmProfile", "n": 1, "one": "DLP EDM profile", "many": "DLP EDM profiles", "names": ["EDM A"]},
             "items": ["EDM A: Internal error while validating the rule"]}
        g = self.pr.guide(f)
        self.assertEqual(g["title"], "Check 1 DLP EDM profile in the CMA")
        self.assertEqual(g["items"], ["EDM A"])
        self.assertIn("check that each item", g["steps"][0])

    def test_cant_confirm_box_lists_only_what_applies(self):
        rep = pc.Report()
        out = self.pr.render_customer_html(rep, export(rules=[rule("wanNetwork", "A")]), self._meta())
        self.assertNotIn("trial", out.split("What this check can't confirm")[1].split("</div>")[0])
        rep.add("access", "WARN", "access_trial", {}, "t")
        out = self.pr.render_customer_html(rep, export(rules=[rule("wanNetwork", "A")]), self._meta())
        self.assertIn("because the new account is a trial", out)

    def test_scim_items_are_not_repeated(self):
        rep = pc.Report()
        rep.add("identity", "BLOCKER", "id_missing", {"type": "SCIM", "n": 1, "groups": 1, "users": 0}, "x",
                items=["user group 'G'"], rules=["internetFirewall: A"])
        rep.add("identity", "ACTION", "id_scim_inactive", {}, "y")
        shown = [f["key"] for f in self.pr._shown(rep)]
        self.assertEqual(shown, ["id_missing"])

    def test_runbook_commands_name_this_pair_of_accounts(self):
        rep = pc.Report()
        rep.add("target", "ACTION", "tgt_clash", {"n": 2, "families": ["internetFirewall", "socketLan"]}, "c",
                items=["internetFirewall: 1 (X)", "socketLan: 1 (Y)"])
        rep.add("features", "ACTION", "feat_wifi", {"n": 1}, "w", items=["HQ:Corp"])
        rep.add("features", "ACTION", "feat_l7", {"n": 1, "sites": ["HQ"]}, "l", items=["NR / FW (sites: HQ)"])
        rep.add("identity", "BLOCKER", "id_missing", {"type": "SCIM", "n": 1, "groups": 1, "users": 0}, "x",
                items=["user group 'G'"], rules=["internetFirewall: A"])
        exp = export(rules=[rule("wanNetwork", "A")])
        phases = self.pr.build_runbook(rep, self._meta())
        steps = {s["id"]: s for _, st in phases for s in st}
        runs = [c for s in steps.values() for c in s["runs"]]
        files = [s["file"] for s in steps.values() if s.get("file")]
        # Step 1 installs the CLI the way the getting started guide does, or step 2's catocli isn't found.
        self.assertEqual(steps["tools"]["cmds"][2:], ["python3 -m venv .venv && .venv/bin/pip install catocli",
                                                      'export PATH="$PWD/.venv/bin:$PATH"'])
        self.assertEqual(steps["keys"]["cmds"][:2], [
            "catocli configure set --profile src-ro --cato-token 'CURRENT_KEY' --account-id 111",
            "catocli configure set --profile 'new acct' --cato-token 'NEW_KEY' --account-id 222"])
        for sid in ("tools", "keys"):
            self.assertIn("Already done on the machine that made this report", steps[sid]["note"])
        self.assertEqual(steps["scim"]["cmds"][0],
                         "catocli configure set --profile 'new acct' --scim-url 'SCIM_URL' --scim-token 'SCIM_TOKEN'")
        self.assertFalse([c for s in steps.values() for c in s["cmds"]
                          if re.fullmatch(r"catocli configure set --profile \S+", c)])  # no bare prompts
        self.assertEqual(steps["purge"]["cmds"], ["sh preflight/migrate.sh clear-samples"])
        self.assertIn("python3 scripts/purge_all.py --profile 'new acct' --only internet_firewall,lan_firewall --execute", runs)
        self.assertIn("sh preflight/scope_command.sh", steps["leaveout"]["runs"])
        self.assertTrue(steps["foundations"]["runs"][0].startswith(
            "python3 scripts/bulk_apply.py --profile 'new acct' --mode deploy --config-dir config_data_target --folders 0-dependencies"))
        self.assertTrue(any("--lan-l7-confirmed" in c for c in steps["l7"]["runs"]))
        self.assertFalse(any("--mirror" in c for c in runs))
        self.assertEqual(files, ['wifi_ssid_passkeys = {\n  "HQ:Corp" = ""\n}'])
        mirrored = self.pr.build_runbook(rep, self._meta(mirror=True))
        self.assertTrue(all("--mirror" in c for _, st in mirrored for s in st for c in s["runs"] if "bulk_apply" in c))
        out = self.pr.render_customer_html(rep, exp, self._meta())
        self.assertIn("Running the migration", out)
        self.assertIn('<div class="before">Before this step: item', out)
        self.assertIn("What it runs (for reference)", out)
        md = "\n".join(self.pr.render_runbook_md(phases, {"feat_wifi": "Provide passwords"}))
        self.assertIn("```bash\nsh preflight/migrate.sh leave-out\n```", md)
        self.assertEqual(self.pr.build_runbook(rep, {"source": "", "target": ""}), [])  # no run details, no runbook

    def test_chapters_and_every_link_lands(self):
        import re
        rep = pc.Report()
        rep.add("pops", "BLOCKER", "pop_missing", {"n": 1, "names": ["P"]}, "x", items=["P"], rules=["wanNetwork: A"])
        rep.add("features", "ACTION", "feat_wifi", {"n": 1}, "w", items=["HQ:Corp"])
        rep.add("target", "ACTION", "tgt_clash", {"n": 1, "families": ["internetFirewall"]}, "c", items=["internetFirewall: 1 (X)"])
        rep.add("export", "OK", "export_ok", {"rules": 2, "policies": 1}, "z")
        out = self.pr.render_customer_html(rep, export(rules=[rule("wanNetwork", "A"), rule("wanNetwork", "B")]), self._meta())
        ids = set(re.findall(r'\sid="([^"]+)"', out))
        hrefs = set(re.findall(r'href="#([^"]+)"', out))
        self.assertFalse(hrefs - ids, f"links with no target: {sorted(hrefs - ids)}")
        for chapter in ("summary", "plan", "ready", "cant", "part2", "phase-1", "appendix-a", "appendix-b", "item-1", "step-1"):
            self.assertIn(chapter, ids)
            self.assertIn(chapter, hrefs | {"item-1", "step-1"})
        self.assertIn('class="toc-side"', out)
        self.assertIn('<a href="#item-1">1</a>', out)  # Appendix A and "Before this step" link to the item
        self.assertIn('<a href="#step-', out)  # "redo step N" in the run book links to that step

    def test_modified_logo_is_left_out(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "cato_logo_rgb_white_art_101.svg"), "w") as fh:
                fh.write("<svg/>")
            old, self.pr.ASSETS = self.pr.ASSETS, d
            try:
                self.assertEqual(self.pr._logo("cato_logo_rgb_white_art_101.svg"), "")
            finally:
                self.pr.ASSETS = old
        self.assertIn("data:image/svg+xml;base64", self.pr._logo("cato_logo_rgb_white_art_101.svg"))


if __name__ == "__main__":
    unittest.main()


class GettingStartedGuide(unittest.TestCase):
    """The one-page guide: branded, and every flag it tells people to type really exists."""

    def test_guide_renders_and_its_flags_exist(self):
        import re
        import subprocess
        import getting_started as gs
        out = gs.render_html()
        import preflight_report as pr
        for needle in ("Getting Started Guide to Account Migration", 'class="cato-logo"', "@font-face",
                       "Gather two API keys", "Run the check", "What to do next", pr._esc(gs.block([gs.CHECK]))):
            self.assertTrue(needle in out, f"missing from the guide: {needle}")
        scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
        for script, line in (("preflight_check.py", gs.CHECK), ("export_source.py", gs.EXPORT[0])):
            helptext = subprocess.run([sys.executable, os.path.join(scripts, script), "--help"],
                                      capture_output=True, text=True).stdout
            for flag in re.findall(r"(--[a-z-]+)", line):
                self.assertIn(flag, helptext, f"{script} has no {flag}")


class PastedCommands(unittest.TestCase):
    """Every printed command is short and chained with "&&", so it pastes the same in any PDF viewer."""

    def test_guide_blocks_paste_the_same_with_or_without_line_breaks(self):
        # Some PDF viewers copy line breaks, others turn them into spaces: a guide block must run the same either way.
        import shlex
        import subprocess
        import getting_started as gs
        for lines, sep in ((gs.ONCE_PER_COMPUTER, "&&"), (gs.ONCE_PER_PAIR, "&&"), (gs.EXPORT, gs.SEPARATOR["EXPORT"]),
                           ([gs.CHECK], "&&")):
            text = gs.block(lines, sep)
            for line in text.splitlines():
                self.assertLessEqual(len(line), gs.LINE_MAX, line)  # never wraps, so never needs a continuation
                self.assertFalse(line.endswith("\\"), line)
            flat = text.replace("\n", " ")
            self.assertEqual(shlex.split(text), shlex.split(flat))
            if sep == "&&":
                self.assertEqual(shlex.split(flat).count("&&"), sum(c.count("&&") for c in lines) + len(lines) - 1)
            else:
                self.assertEqual(text.count(";\n"), len(lines) - 1)
            for form in (text, flat):
                self.assertEqual(subprocess.run(["bash", "-n", "-c", form]).returncode, 0, form)
                self.assertEqual(subprocess.run(["zsh", "-n", "-c", form]).returncode, 0, form)
        self.assertIn("white-space:pre;", gs.render_html())

    META = {"when": "2026-11-24", "source": "Src (1234567)", "target": "Tgt (7654321)",
            "run": {"source_profile": "current-acct", "target_profile": "new-acct", "source_account": "1234567",
                    "target_account": "7654321", "config_dir": "config_data", "target_config_dir": "config_data_target",
                    "out_dir": "preflight", "probe": True, "customer": "Example Co", "lan_l7_confirmed": False,
                    "provider_version": "2.0.0", "mirror": True}}

    def _phases(self):
        import preflight_report as pr
        rep = pc.Report()
        for k, ctx, items in (("feat_l7", {"n": 1, "sites": ["S"]}, ["x"]), ("feat_wifi", {"n": 1}, ["S:W"]),
                              ("tgt_clash", {"n": 1, "families": ["internetFirewall"]}, ["internetFirewall: 1 (x)"]),
                              ("id_missing", {"type": "SCIM", "n": 1, "groups": 1, "users": 0}, ["user group 'G'"]),
                              ("int_classic", {"n": 1}, ["Legacy: 1 rule(s)"])):
            rep.add("features", "ACTION", k, ctx, "t", items=items)
        return pr, pr.build_runbook(rep, self.META)

    def test_every_printed_command_is_short_and_pastes_either_way(self):
        import shlex
        import subprocess
        pr, phases = self._phases()
        for _, st in phases:
            for s in st:
                if not s["cmds"]:
                    continue
                text = pr.chain(s["cmds"], s.get("sep", "&&"))
                for line in text.splitlines():
                    self.assertLessEqual(len(line), pr.CMD_LINE_MAX, line)  # prints whole, never needs a continuation
                    self.assertFalse(line.endswith("\\"), line)
                flat = text.replace("\n", " ")
                self.assertEqual(shlex.split(text), shlex.split(flat))
                for form in (text, flat):
                    self.assertEqual(subprocess.run(["bash", "-n", "-c", form]).returncode, 0, form)
                    self.assertEqual(subprocess.run(["zsh", "-n", "-c", form]).returncode, 0, form)

    def test_migrate_script_runs_its_steps(self):
        import json
        import subprocess
        import tempfile
        pr, phases = self._phases()
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "preflight"))
            os.makedirs(os.path.join(repo, "bin"))
            log = os.path.join(repo, "calls.log")
            with open(os.path.join(repo, "bin", "python3"), "w") as fh:  # stand-in: records each call, changes nothing
                fh.write(f"#!/bin/sh\necho \"python3 $*\" >> {log}\n")
            os.chmod(os.path.join(repo, "bin", "python3"), 0o755)
            self.assertEqual(pr.write_migrate_sh(phases, repo, os.path.join(repo, "preflight"), self.META), "migrate.sh")
            script = os.path.join(repo, "preflight", "migrate.sh")
            env = dict(os.environ, PATH=os.path.join(repo, "bin") + os.pathsep + os.environ["PATH"])

            def step(*args, answer=""):
                return subprocess.run(["sh", script, *args], cwd="/", env=env, input=answer, text=True, capture_output=True)

            self.assertEqual(subprocess.run(["sh", "-n", script]).returncode, 0)
            listing = step("list").stdout
            for sub in ("guard", "check", "check-groups", "copy-users", "clear-samples", "leave-out", "foundations",
                        "policies", "replan", "export-new", "compare", "bring-back", "bring-back-l7", "resume FOLDER"):
                self.assertIn(sub, listing)
            step("guard")  # runs from anywhere: the script moves to the repo first
            with open(os.path.join(repo, "cbcm.config.json")) as fh:
                self.assertEqual(json.load(fh), {"protected_accounts": ["1234567"], "protected_profiles": ["current-acct"]})
            r = step("clear-samples", answer="n\n")  # a "no" stops before anything is deleted
            self.assertIn("Not done.", r.stdout)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("purge_all.py --profile new-acct --only internet_firewall\n", calls)
            self.assertNotIn("--execute", calls)
            step("clear-samples", answer="y\n")
            with open(log) as fh:
                self.assertIn("--only internet_firewall --execute", fh.read())
            r = step("resume", "4-policies/wan-network")
            with open(log) as fh:
                self.assertIn("--keep-going --start-at 4-policies/wan-network", fh.read())
            self.assertNotEqual(step("no-such-step").returncode, 0)
        exports = [c for _, st in phases for s in st if s["id"] == "export_target" for c in s["runs"]]
        types = ",".join(c.split("--types ")[1] for c in exports if "--types " in c).split(",")
        self.assertEqual(sorted(types), sorted(pr.EXPORT_TYPES))  # every type still exported once


FAKE_CATOCLI = r'''#!PYTHON
"""Stand-in for catocli 3.3.0's export: aborts `export all` at the first type in $DENY, like the real one."""
import os, sys
TYPES = ["a_rules", "groups", "sb_rules", "wf_rules", "wnw_rules"]
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as fh:
    fh.write(" ".join(args) + "\n")
deny = [t for t in os.environ.get("DENY", "").split(",") if t]
if args[:2] == ["export", "all"]:
    types = args[args.index("--types") + 1].split(",") if "--types" in args else TYPES
    if types == ["_"]:
        print("ERROR: --types: unknown export type(s): _. Valid types: " + ", ".join(TYPES))
        sys.exit(1)
    for i, t in enumerate(types, 1):
        print(f"[{i}/{len(types)}] {t}: exporting...")
        if t in deny:
            print(f"[{i}/{len(types)}] {t}: FAILED (Unexpected error during API call - API returned errors: Access "
                  f"denied: Insufficient privileges for account 1234567)")
            if os.environ.get("NOABORT"):
                continue
            print(f"\nABORTING: the API key does not have sufficient permissions for this operation (access denied "
                  f"on '{t}') - skipping the remaining {len(types) - i} export type(s).")
            sys.exit(1)
        print(f"[{i}/{len(types)}] {t}: OK -> config_data/{t}.json (+ stripped)")
    sys.exit(1 if deny and os.environ.get("NOABORT") else 0)
if args[:2] == ["export", "static_hosts"]:
    print('{"success": true}')
    sys.exit(0)
sys.exit(2)
'''


class ExportSource(unittest.TestCase):
    """export_source.py carries on past types the key is refused, and records them for the check."""

    def _run(self, deny="", noabort=False):
        import json
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        os.makedirs(os.path.join(d, "bin"))
        fake = os.path.join(d, "bin", "catocli")
        with open(fake, "w") as fh:
            fh.write(FAKE_CATOCLI.replace("PYTHON", sys.executable))
        os.chmod(fake, 0o755)
        env = dict(os.environ, PATH=os.path.join(d, "bin") + os.pathsep + os.environ["PATH"], DENY=deny,
                   CALLS=os.path.join(d, "calls.log"), NOABORT="1" if noabort else "")
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "export_source.py")
        r = subprocess.run([sys.executable, script, "--profile", "src", "--config-dir", os.path.join(d, "out"),
                            "--no-webhooks"], env=env, capture_output=True, text=True)
        with open(os.path.join(d, "out", "export_report.json")) as fh:
            report = json.load(fh)
        with open(os.path.join(d, "calls.log")) as fh:
            calls = fh.read().splitlines()
        return r, report, calls

    def test_a_refused_type_is_skipped_and_the_rest_still_export(self):
        r, report, calls = self._run(deny="sb_rules")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x["type"] for x in report["refused"]], ["sb_rules"])
        self.assertEqual(report["failed"], [])
        for t in ("a_rules", "groups", "wf_rules", "wnw_rules", "static_hosts --site-type all"):
            self.assertIn(t, report["exported"])
        self.assertTrue(any(c.endswith("--types wf_rules,wnw_rules") for c in calls), calls)  # only what was skipped

    def test_several_refusals_each_get_a_rerun(self):
        _, report, calls = self._run(deny="groups,wf_rules")
        self.assertEqual(sorted(x["type"] for x in report["refused"]), ["groups", "wf_rules"])
        self.assertIn("wnw_rules", report["exported"])
        self.assertEqual(sum(c.startswith("export all --strip-ids") for c in calls), 3)

    def test_a_catocli_that_carries_on_by_itself_needs_one_run(self):
        _, report, calls = self._run(deny="sb_rules", noabort=True)
        self.assertEqual([x["type"] for x in report["refused"]], ["sb_rules"])
        self.assertEqual(sum(c.startswith("export all --strip-ids") for c in calls), 1)

    def test_the_check_reports_refused_types_with_their_permission(self):
        exp = export(rules=[rule("internetFirewall", "A")])
        exp["policy_enabled"] = {"internetFirewall": True}
        exp["export_report"] = {"refused": [{"type": "sb_rules", "error": "Access denied: Insufficient privileges"}],
                                "failed": []}
        src_ok = tenant(policies={"internetFirewall": {"enabled": True, "rules": ["A"]}})
        rep = pc.Report()
        pc.check_permissions(rep, exp, src_ok, tenant(policies={"internetFirewall": {"enabled": True, "rules": []}}))
        f = [x for x in rep.findings if x["key"] == "export_refused"][0]
        self.assertEqual(f["items"], ["sb_rules: needs the 'Socket Bypass' permission"])
        import preflight_report as pr
        self.assertIn("Socket Bypass", pr.guide(f)["steps"][0])
        # When the live read already reports Socket Bypass as unreadable, it isn't listed twice.
        src_denied = tenant(policies={"socketBypass": {"error": "Access denied: Insufficient privileges"}})
        rep = pc.Report()
        pc.check_permissions(rep, exp, src_denied, tenant(policies={}))
        self.assertEqual([x["key"] for x in rep.findings if x["key"] in ("perm_source", "export_refused")], ["perm_source"])
