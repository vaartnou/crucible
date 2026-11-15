import React from 'react';
import { GitCompare, X } from 'lucide-react';
import type { BaselineComparePayload, GodState, ScoreData, SerializedStep } from '../types';
import { ActionTimeline } from './ActionTimeline';

interface CompareModalProps {
  primaryLabel: string;
  primaryScore: ScoreData | null;
  primarySteps: SerializedStep[];
  primaryGod: GodState | null;
  baseline: BaselineComparePayload;
  baselinePolicy: string;
  availablePolicies: string[];
  onChangeBaselinePolicy: (policy: string) => void;
  onClose: () => void;
}

export const CompareDrawer: React.FC<CompareModalProps> = ({
  primaryLabel,
  primaryScore,
  primarySteps,
  primaryGod,
  baseline,
  baselinePolicy,
  availablePolicies,
  onChangeBaselinePolicy,
  onClose,
}) => {
  const pLoss = primaryGod?.timeline.actual_loss ?? [];
  const bLoss = baseline.god_state?.timeline.actual_loss ?? [];
  const baseNoop = baseline.god_state?.timeline.baseline_loss ?? [];
  const maxLen = Math.max(pLoss.length, bLoss.length, baseNoop.length, 2);
  const maxY = Math.max(10, ...pLoss, ...bLoss, ...baseNoop);

  const w = 720;
  const h = 135;
  const xOf = (i: number) => 36 + (i / (maxLen - 1)) * (w - 50);
  const yOf = (v: number) => 12 + (h - 32) - (v / maxY) * (h - 32);
  const toPath = (arr: number[]) =>
    arr.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i).toFixed(1)} ${yOf(v).toFixed(1)}`).join(' ');

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 60,
        background: 'rgba(5, 8, 13, 0.82)',
        backdropFilter: 'blur(4px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 20,
      }}
    >
      <div
        className="card"
        style={{
          width: 'min(1180px, 96vw)',
          height: 'min(840px, 92vh)',
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
          padding: 16,
          background: 'var(--bg-1)',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <GitCompare size={18} color="var(--cyan)" />
            <span style={{ fontWeight: 700, fontSize: 15 }}>
              Paired World Comparison — Seed {baseline.seed}
            </span>
            <span className="badge badge-info">{baseline.task}</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 12, color: 'var(--text-2)' }}>Compare against policy:</span>
            <select
              className="select mono"
              value={baselinePolicy}
              onChange={(e) => onChangeBaselinePolicy(e.target.value)}
            >
              {availablePolicies.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <button className="btn" onClick={onClose}>
              <X size={14} /> Close
            </button>
          </div>
        </div>

        {/* Score Summary Cards */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <ScoreCard
            title={primaryLabel}
            accent="#38bdf8"
            score={primaryScore}
            stepsCount={primarySteps.length}
            endMinute={primarySteps[primarySteps.length - 1]?.minute_after ?? 5}
          />
          <ScoreCard
            title={`Baseline: ${baseline.policy}`}
            accent="#10b981"
            score={baseline.score}
            stepsCount={baseline.all_steps.length}
            endMinute={
              baseline.all_steps[baseline.all_steps.length - 1]?.minute_after ?? 5
            }
          />
        </div>

        {/* Overlaid Counterfactual Loss Chart */}
        {bLoss.length > 0 && (
          <div className="card" style={{ padding: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontWeight: 600, fontSize: 12 }}>
                Synchronized Failed-Request Loss Curve (Simulated Minutes -30 → End)
              </span>
              <div className="mono" style={{ display: 'flex', gap: 14, fontSize: 11 }}>
                <span style={{ color: '#38bdf8' }}>━ {primaryLabel}</span>
                <span style={{ color: '#10b981' }}>━ {baseline.policy}</span>
                <span style={{ color: '#ef4444' }}>┅ do_nothing</span>
              </div>
            </div>
            <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`}>
              <path
                d={toPath(baseNoop)}
                fill="none"
                stroke="#ef4444"
                strokeWidth={1.4}
                strokeDasharray="4 3"
                opacity={0.7}
              />
              {pLoss.length > 0 && (
                <path d={toPath(pLoss)} fill="none" stroke="#38bdf8" strokeWidth={2.2} />
              )}
              <path d={toPath(bLoss)} fill="none" stroke="#10b981" strokeWidth={2.2} />
            </svg>
          </div>
        )}

        {/* Side-by-side Action Timelines */}
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: 12,
          }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
            <div style={{ fontWeight: 600, marginBottom: 6, color: '#38bdf8' }}>
              {primaryLabel} ({primarySteps.length} calls)
            </div>
            <div style={{ flex: 1, overflowY: 'auto', paddingRight: 4 }}>
              <ActionTimeline steps={primarySteps} selectedStep={null} />
            </div>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
            <div style={{ fontWeight: 600, marginBottom: 6, color: '#10b981' }}>
              {baseline.policy} ({baseline.all_steps.length} calls)
            </div>
            <div style={{ flex: 1, overflowY: 'auto', paddingRight: 4 }}>
              <ActionTimeline steps={baseline.all_steps} selectedStep={null} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

const ScoreCard: React.FC<{
  title: string;
  accent: string;
  score: ScoreData | null;
  stepsCount: number;
  endMinute: number;
}> = ({ title, accent, score, stepsCount, endMinute }) => (
  <div className="card" style={{ borderLeft: `3px solid ${accent}` }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <span style={{ fontWeight: 700, fontSize: 13.5 }}>{title}</span>
      <span className="mono" style={{ fontSize: 16, fontWeight: 700, color: accent }}>
        {score ? score.total.toFixed(4) : 'In Progress'}
      </span>
    </div>
    <div
      className="mono"
      style={{
        display: 'flex',
        gap: 12,
        flexWrap: 'wrap',
        marginTop: 6,
        fontSize: 11,
        color: 'var(--text-1)',
      }}
    >
      <span>calls: {stepsCount}</span>
      <span>clock: m={endMinute}</span>
      {score && (
        <>
          <span>impact: {(score.breakdown.impact ?? 0).toFixed(3)}</span>
          <span>verified: {score.flags.verified ? 'YES' : 'NO'}</span>
          <span>diagnosis: {((score.breakdown.diagnosis ?? 0) * 100).toFixed(0)}%</span>
          <span>penalty: -{(score.breakdown.penalty ?? 0).toFixed(2)}</span>
        </>
      )}
    </div>
  </div>
);
