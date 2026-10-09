-- 013 — System-global predicate seed (~88 predicates).
--
-- All inserted with tenant_id=NULL → visible to every tenant via the read
-- policy in 012. Custom per-tenant predicates are added by the app at runtime.
-- New globals = a future migration (014_predicates_more.sql, etc.) that
-- INSERT new rows. Existing globals stay.
--
-- Aliases capture common surface forms so the LLM extractor can normalize
-- "is priced at" → costs without the user having to teach it. Aliases are
-- additive — extending a predicate's aliases is one UPDATE.

-- ─── attribute (subject HAS-X object) ──────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'costs',         'monetary cost of subject',     ARRAY['is priced at','is sold for','retails for','priced at'], 'attribute'),
(NULL, 'weighs',        'weight measurement',           ARRAY['has a weight of','weighs in at'], 'attribute'),
(NULL, 'measures',      'dimension measurement',        ARRAY['is sized at','has dimensions of'], 'attribute'),
(NULL, 'has_count',     'cardinality of items',         ARRAY['contains count','has a count of'], 'attribute'),
(NULL, 'has_quantity',  'amount of substance',          ARRAY['has an amount of'], 'attribute'),
(NULL, 'has_color',     'color attribute',              ARRAY['is colored','is the color'], 'attribute'),
(NULL, 'has_size',      'size attribute',               ARRAY['is sized'], 'attribute'),
(NULL, 'has_status',    'state attribute',              ARRAY['is in state of'], 'attribute');

-- ─── relation (subject KNOWS-OF object) ────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'is_a',              'type-of relationship',         ARRAY['is type of','is an','is the','is kind of'], 'relation'),
(NULL, 'works_at',          'employment relationship',      ARRAY['employed by','is employed by','works for'], 'relation'),
(NULL, 'lives_in',          'residence',                    ARRAY['resides in','is based in','lives at'], 'relation'),
(NULL, 'born_in',           'birthplace',                   ARRAY['was born in','originates from'], 'relation'),
(NULL, 'owns',              'ownership',                    ARRAY['has ownership of','possesses'], 'relation'),
(NULL, 'belongs_to',        'membership in a group',        ARRAY['is part of','member of','is one of'], 'relation'),
(NULL, 'married_to',        'marital relationship',         ARRAY['spouse of','wife of','husband of','partner of'], 'relation'),
(NULL, 'parent_of',         'parental relationship',        ARRAY['is parent of','father of','mother of'], 'relation'),
(NULL, 'siblings_with',     'sibling relationship',         ARRAY['brother of','sister of'], 'relation'),
(NULL, 'dating',            'romantic relationship (non-marital)', ARRAY['in a relationship with','seeing','partner'], 'relation'),
(NULL, 'roommates_with',    'co-resident',                  ARRAY['lives with','flatmate of'], 'relation'),
(NULL, 'friend_of',         'friendship',                   ARRAY['is friends with','close to'], 'relation'),
(NULL, 'knows',             'acquaintance',                 ARRAY['is acquainted with','familiar with'], 'relation'),
(NULL, 'reports_to',        'reporting / org structure',    ARRAY['answers to','reports into'], 'relation'),
(NULL, 'manages',           'inverse of reports_to',        ARRAY['supervises','leads','directs'], 'relation'),
(NULL, 'mentors',           'mentor-of relationship',       ARRAY['coaches','advises','guides'], 'relation'),
(NULL, 'mentored_by',       'mentee-of relationship',       ARRAY['coached by','advised by'], 'relation'),
(NULL, 'collaborates_with', 'collaboration relationship',   ARRAY['works with','partners with'], 'relation'),
(NULL, 'is_member_of',      'organization membership',      ARRAY['member of','part of'], 'relation'),
(NULL, 'depends_on',        'dependency relationship',      ARRAY['relies on','requires','needs'], 'relation'),
(NULL, 'supports',          'enables / supports',           ARRAY['enables','facilitates','allows'], 'relation'),
(NULL, 'integrates_with',   'integration relationship',     ARRAY['connects to','works with','plays with'], 'relation'),
(NULL, 'powered_by',        'underlying tech',              ARRAY['uses','built with','runs on'], 'relation'),
(NULL, 'authored_by',       'creator / author',             ARRAY['written by','created by','made by','built by'], 'relation'),
(NULL, 'sold_by',           'merchant relationship',        ARRAY['available from','offered by'], 'relation');

-- ─── temporal (subject DID-AT object) ──────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'started_at',     'temporal start',           ARRAY['began at','commenced at','began on'], 'temporal'),
(NULL, 'ended_at',       'temporal end',             ARRAY['concluded at','finished at','ended on'], 'temporal'),
(NULL, 'occurred_at',    'point in time event',      ARRAY['happened at','took place at','occurred on'], 'temporal'),
(NULL, 'due_at',         'deadline',                 ARRAY['is due','must complete by','due on','deadline of'], 'temporal'),
(NULL, 'scheduled_for',  'planned time',             ARRAY['planned for','set for'], 'temporal'),
(NULL, 'created_at',     'creation time',            ARRAY['was created','came into existence'], 'temporal'),
(NULL, 'modified_at',    'last modification time',   ARRAY['last edited','last changed'], 'temporal'),
(NULL, 'lasted',         'duration',                 ARRAY['took','endured for','was for'], 'temporal'),
(NULL, 'born_on',        'birth date (recurring annually)', ARRAY['birthday','was born on','dob'], 'temporal'),
(NULL, 'anniversary_of', 'recurring annual marker',  ARRAY['anniversary','marks','commemorates'], 'temporal');

-- ─── quantitative ─────────────────────────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'requires',  'requirement count or amount',  ARRAY['needs','demands','calls for','requires us to'], 'quantitative'),
(NULL, 'produces',  'output quantity',              ARRAY['yields','generates','outputs','results in'], 'quantitative'),
(NULL, 'consumes',  'consumption quantity',         ARRAY['uses','expends','draws','consumes'], 'quantitative');

-- ─── locational ───────────────────────────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'located_in',      'physical location',     ARRAY['situated in','found in','is at','position in'], 'locational'),
(NULL, 'headquartered_in','HQ location',           ARRAY['based in','HQ in','main office in'], 'locational'),
(NULL, 'travels_to',      'movement towards',      ARRAY['goes to','heading to','traveling to'], 'locational'),
(NULL, 'travels_from',    'origin of movement',    ARRAY['comes from','arrived from'], 'locational');

-- ─── communicative ────────────────────────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'said',         'attribution of statement',  ARRAY['stated','remarked','noted','mentioned'], 'communicative'),
(NULL, 'claimed',      'asserted',                  ARRAY['asserted','contended','argued'], 'communicative'),
(NULL, 'decided',      'made a decision',           ARRAY['concluded','chose','resolved'], 'communicative'),
(NULL, 'agreed_to',    'consent / agreement',       ARRAY['signed off on','approved','accepted'], 'communicative'),
(NULL, 'denied',       'rejection',                 ARRAY['refused','rejected','declined'], 'communicative'),
(NULL, 'asked',        'inquiry',                   ARRAY['inquired','requested','queried'], 'communicative'),
(NULL, 'recommended',  'suggestion',                ARRAY['suggested','advised','proposed'], 'communicative');

-- ─── reference ────────────────────────────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'cites',       'formal citation',           ARRAY['references','points to','cites work of'], 'reference'),
(NULL, 'mentions',    'informal mention',          ARRAY['refers to','calls out','brings up'], 'reference'),
(NULL, 'links_to',    'hyperlink reference',       ARRAY['hyperlinks to','URL points to'], 'reference'),
(NULL, 'builds_on',   'extends prior work',        ARRAY['extends','expands','elaborates on'], 'reference'),
(NULL, 'replaces',    'supersedes prior thing',    ARRAY['supersedes','obsoletes','succeeds'], 'reference');

-- ─── state ────────────────────────────────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'is_active',     'current/ongoing state',         ARRAY['is active','currently active','active state'], 'state'),
(NULL, 'is_done',       'completed state',               ARRAY['is complete','completed','finished'], 'state'),
(NULL, 'is_dormant',    'paused state',                  ARRAY['is paused','on hold','dormant'], 'state'),
(NULL, 'is_planned',    'planned future state',          ARRAY['is planned','scheduled','upcoming'], 'state'),
(NULL, 'is_deprecated', 'no longer used/recommended',    ARRAY['deprecated','retired','obsolete'], 'state'),
(NULL, 'is_blocked',    'currently blocked',             ARRAY['blocked by','stuck on'], 'state');

-- ─── preference (emotional/cognitive; treated as 'opinion' conflict type) ─
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'prefers',  'preference',                  ARRAY['would rather','partial to','tends to choose'], 'preference'),
(NULL, 'likes',    'positive affinity',           ARRAY['is fond of','enjoys','digs','is into'], 'preference'),
(NULL, 'dislikes', 'negative affinity',           ARRAY['is not a fan of','averse to'], 'preference'),
(NULL, 'loves',    'strong positive affinity',    ARRAY['adores','is passionate about'], 'preference'),
(NULL, 'hates',    'strong negative affinity',    ARRAY['despises','cannot stand','loathes'], 'preference'),
(NULL, 'enjoys',   'derives pleasure from',       ARRAY['takes pleasure in','has fun with'], 'preference'),
(NULL, 'favours',  'preferred-over alternative',  ARRAY['favors','goes for','picks'], 'preference'),
(NULL, 'believes', 'cognitive position',          ARRAY['holds that','is convinced','considers true'], 'preference'),
(NULL, 'thinks',   'opinion',                     ARRAY['is of the view','feels that'], 'preference'),
(NULL, 'suspects', 'tentative belief',            ARRAY['has a hunch','guesses','reckons'], 'preference');

-- ─── other (transitive action verbs) ──────────────────────────────────────
INSERT INTO predicates (tenant_id, canonical, description, aliases, kind) VALUES
(NULL, 'built',     'created/constructed',     ARRAY['constructed','assembled','made'], 'other'),
(NULL, 'designed',  'design action',           ARRAY['conceived','sketched','laid out'], 'other'),
(NULL, 'wrote',     'authoring action',        ARRAY['authored','penned','drafted'], 'other'),
(NULL, 'sold',      'sale transaction',        ARRAY['vended','offloaded'], 'other'),
(NULL, 'bought',    'purchase transaction',    ARRAY['purchased','acquired'], 'other'),
(NULL, 'hired',     'employment action',       ARRAY['onboarded','employed'], 'other'),
(NULL, 'fired',     'employment termination',  ARRAY['let go','dismissed','terminated'], 'other'),
(NULL, 'launched',  'release / debut',         ARRAY['shipped','released','debuted','introduced'], 'other'),
(NULL, 'cancelled', 'cancellation',            ARRAY['called off','aborted'], 'other'),
(NULL, 'moved',     'relocation action',       ARRAY['relocated','transferred'], 'other');


INSERT INTO schema_migrations (version) VALUES ('013_predicates_seed');
