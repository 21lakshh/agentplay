import { Pause, Play, SkipBack } from '@phosphor-icons/react';
import { Button } from '@/components/ui/button';
import { SPEEDS, type PlaybackState } from '@/core/playback';
import { fmtDur } from '@/core/sessionMapModel';

interface Props {
  state: PlaybackState;
  /** wall clock at the playhead ("HH:MM") */
  clock: string;
  /** ms since session start at the playhead */
  elapsedMs: number;
  durationMs: number;
  onStart: () => void;
  onToggle: () => void;
  onCycleSpeed: () => void;
}

/** The dock's playback group: start, play / pause, speed and the readout.
 * Stepping and the jumps stay on the keyboard (see the replay region). */
export function PlaybackControls({
  state,
  clock,
  elapsedMs,
  durationMs,
  onStart,
  onToggle,
  onCycleSpeed,
}: Props) {
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <Button
        variant="outline"
        size="iconSm"
        onClick={onStart}
        disabled={state.n < 1}
        aria-label="Jump to start"
      >
        <SkipBack size={14} weight="fill" />
      </Button>
      <Button
        size="sm"
        onClick={onToggle}
        disabled={state.n < 2}
        aria-label={state.playing ? 'Pause (Space)' : 'Play (Space)'}
        className="min-w-[76px]"
      >
        {state.playing ? (
          <Pause size={14} weight="fill" />
        ) : (
          <Play size={14} weight="fill" />
        )}
        {state.playing ? 'Pause' : 'Play'}
      </Button>
      <Button
        variant="outline"
        size="sm"
        onClick={onCycleSpeed}
        aria-label="Playback speed"
        className="font-mono"
      >
        {SPEEDS[state.speedIdx]}×
      </Button>
      <span
        className="font-mono ds-text-body-xs-medium text-ds-text-subtle tabular-nums"
        title={`${fmtDur(elapsedMs)} of ${fmtDur(durationMs)}`}
      >
        {state.n
          ? `${(state.p + 1).toLocaleString()}/${state.n.toLocaleString()}`
          : '0/0'}{' '}
        · {clock}
      </span>
    </div>
  );
}
