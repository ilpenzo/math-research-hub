# Algebraic geometry: conventions, model papers and shapes

Contents: 1 Conventions checklist · 2 Distinctions to preserve · 3 Model
papers · 4 Paper shapes · 5 MSC codes

## 1. Conventions checklist

State each convention the paper depends on exactly once. Put it in a
"Conventions" subsection at the end of the introduction or in Section 2,
or at the start of the first section where it is used. The user's
rational-surfaces preprint (v9) is a compact model: field, characteristic,
closed points, D^b(X) = D^b(coh X), global Hom and Ext, derived pullback and
duality, and the ordering convention for semiorthogonal decompositions, all in
one subsection.

- **Base field.** Is k algebraically closed? Which characteristic: 0,
  arbitrary, or p ≠ 2? Do the results hold over non-closed fields?
- **"Variety".** For example, an integral separated scheme of finite type
  over k. Are "points" closed points?
- **Smooth and regular.** These differ over imperfect fields. Also fix
  projective versus complete versus proper, and normal or Q-factorial if used.
- **Sheaves.** A vector bundle is a locally free sheaf of finite rank. Say
  whether you identify it with the geometric bundle, and in which direction.
  Distinguish torsion-free, reflexive and locally free; distinguish the fibre
  G(x) from the stalk.
- **Projectivization.** Is P(E) = Proj Sym E (Grothendieck: rank-one
  quotients) or the bundle of lines in E? Which O(1)? This matters in every
  projective-bundle paper.
- **Derived category.** D^b(X) = D^b(coh X). Either write derived functors
  explicitly (Lf^*, Rf_*, ⊗^L) or declare that all functors are derived. Fix
  the duality RHom(-, O_X) and the Serre functor S_X = (-) ⊗ ω_X[dim X].
- **Exceptional collections.** Hom(E, E) = k and Ext^i(E, E) = 0 for i ≠ 0.
  - Ordering: Ext^•(E_j, E_i) = 0 for j > i.
  - Strong: no higher Ext in either direction.
  - Full: generates D^b(X). Say whether as a triangulated category closed
    under direct summands.
  - Also define tilting objects, and keep maximal length separate from full.
- **Semiorthogonal decompositions.** Fix which way Hom vanishes in
  ⟨A, B⟩.
- **Mutations.** Fix left and right conventions with their defining triangles
  and shifts, and normalize to degree zero when the objects are sheaves.
- **Genericity.** "General" means on a dense open subset; "very general" means
  outside a countable union of proper closed subsets. Keep arbitrary centres
  apart from general ones, and handle infinitely near points, strict and total
  transforms, and the order of blow-ups.
- **Toric.** Fix the lattices N and M, the fan Σ in N_R, the rays ρ and the
  torus-invariant divisors D_ρ, the sign convention for sections of O(D) as
  lattice points, and the Picard rank (number of rays minus dimension, for
  smooth complete varieties).
- **Numerical.** The Euler form χ(E, F) = Σ (-1)^i dim Ext^i(E, F), Chern
  class conventions, and K_0 versus numerical K-theory.
- **Stability.** The polarization H, slope versus Gieseker stability, and
  μ = c_1·H^{n-1}/rank.

## 2. Distinctions to preserve

The title, abstract and introduction keep exactly the qualifiers of the
theorems:
- exceptional versus strong; full versus maximal length;
- tilting bundle versus exceptional summands;
- vector bundle versus coherent sheaf versus complex;
- local (fibre or stalk) versus global maps;
- general versus arbitrary centres; characteristic 0 versus arbitrary
  characteristic;
- line bundles versus higher-rank bundles;
- numerical data versus actual objects.

A control or example in one category says nothing about another unless its
hypotheses hold there too.

## 3. Model papers

Each was checked on 2026-10-14 against its arXiv abstract and the
introduction of the arXiv PDF. Imitate the device, not the prose.

| Paper | Model for |
|---|---|
| Kawamata, *Derived categories of toric varieties*, math/0503102 | Theorem 1.1 in the first paragraph; a one-paragraph strategy (toric MMP, induction on dimension); explains why the stacky generality is forced |
| Totaro, *Hypersurfaces that are not stably rational*, arXiv:1502.04040 | A 10-page note. Precise comparison with Kollár's range ("we get a bit more"); a short Notation section defining nonstandard terms; one idea paragraph; an open question at the end of the introduction |
| Bridgeland, *Stability conditions on triangulated categories*, math/0212237 | An example (curves, Harder–Narasimhan filtrations) before the abstract definition; a sentence on what the paper does not do |
| Bridgeland, King and Reid, *Mukai implies McKay*, math/9908027 | Precise credit to each earlier author; the setup diagram before the theorem; a remark that the hypothesis fails in dimension at least 4; points to the one nontrivial section |
| Hille and Perling, *Exceptional sequences of invertible sheaves on rational surfaces*, arXiv:0810.1936 | A "key idea" paragraph; the introduction quotes body theorems under their body numbers; an overview paragraph |
| Efimov, *Maximal lengths of exceptional collections of line bundles*, arXiv:1010.3755 | Conjecture, then counterexample theorem, positive complement and open conjecture, all on one counter; flags the technical part; thanks the reader who found a mistake |
| Lazarsfeld and Mustață, *Convex bodies associated to linear series*, arXiv:0805.4559 | Headline Theorems A and B; says that the introduction simplifies hypotheses; a worked example with a figure; sections of examples and open problems |
| Vakil, *Murphy's law in algebraic geometry*, math/0411469 | A broad theorem stated as a slogan and then precisely; a "Philosophy" subsection; history and questions |
| Kuznetsov, *Semiorthogonal decompositions in algebraic geometry* (ICM 2014), arXiv:1404.3143 | The survey format: roadmap, named and attributed theorems, a scope disclaimer |
| Bondal and Orlov, *Semiorthogonal decompositions for algebraic varieties*, alg-geom/9506012 | A counter-model: the abstract and introduction list topics without stating a theorem, so the paper reads as a catalogue |

Common features: abstracts of 85 to 135 words that state the result; the main
theorem by page 2; precise attribution; a short idea-of-proof passage; a
roadmap that flags routine sections; conventions placed where they are needed.

## 4. Paper shapes

- **Research article** (15–40 pages). A contents list is optional. The
  introduction runs 3 to 6 pages with the full architecture. Conventions go in
  a subsection or Section 2; routine checks go in an appendix.
- **Short note** (up to 12 pages). No contents list. The theorem comes in the
  first two paragraphs, followed by one idea paragraph, compressed notation
  and one main proof (Totaro, Kawamata).
- **Foundational or long paper.** A contents list; Theorems A, B, ...; an
  examples section; an open-problems section (Lazarsfeld–Mustață,
  Bridgeland).
- **Counterexample paper.** The attributed conjecture first, then the
  counterexample family, sharpness, and a positive complement (Efimov).
- **Expository companion,** for discussion with experts rather than posting.
  Two to four pages:
  - a one-minute pitch;
  - the mechanism in words;
  - the precise difference from the closest earlier work;
  - focused technical questions.

## 5. Example MSC 2020 codes (algebraic geometry)

Confirm each description at https://msc2020.org before use.
- 14F08 Derived categories of sheaves, dg categories and related
  constructions in algebraic geometry. This replaced 14F05 for such work from
  2020.
- 14M25 Toric varieties, Newton polyhedra, Okounkov bodies.
- 14J60 Vector bundles on surfaces and higher-dimensional varieties, and their
  moduli.
- 14J26 Rational and ruled surfaces.
- 18G80 Derived categories, triangulated categories.

