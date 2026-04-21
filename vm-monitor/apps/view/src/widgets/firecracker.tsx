import type { Config, FirecrackerLoad, FirecrackerVm, Transient } from '@dashdot/common';
import { faFire } from '@fortawesome/free-solid-svg-icons';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { AnimatePresence, motion } from 'framer-motion';
import { type FC } from 'react';
import styled, { useTheme } from 'styled-components';

// ── Styled Components ──────────────────────────────────────────────────────────

const Container = styled.div`
  position: relative;
  display: flex;
  flex-direction: column;
  width: 100%;
  padding: 20px 25px 25px;
  gap: 22px;
`;

const Header = styled.div`
  display: flex;
  flex-direction: row;
  align-items: flex-start;
  gap: 20px;
`;

const IconBox = styled.div<Transient<{ color: string }>>`
  display: flex;
  align-items: center;
  justify-content: center;
  width: 80px;
  height: 80px;
  min-width: 80px;
  position: relative;
  top: -30px;
  background-color: ${(p) => p.$color};
  border-radius: 10px;
  box-shadow: 13px 13px 35px 0px rgba(0, 0, 0, 0.15);
  flex-shrink: 0;
  transition: background-color 0.5s ease;

  svg {
    color: ${(p) => p.theme.colors.text};
  }
`;

const HeadingGroup = styled.div`
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding-top: 12px;
`;

const Heading = styled.h2`
  font-size: 1.5rem;
  font-weight: bold;
  color: ${(p) => p.theme.colors.text};
  margin: 0;
`;

const SubHeading = styled.p`
  font-size: 0.875rem;
  color: ${(p) => p.theme.colors.text}99;
  margin: 0;
`;

const VmGrid = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
`;

const VmCard = styled(motion.div)<Transient<{ running: boolean }>>`
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 14px 16px;
  border-radius: 12px;
  border: 1px solid
    ${(p) =>
      p.$running
        ? p.theme.colors.vmPrimary + '55'
        : p.theme.colors.text + '18'};
  background-color: ${(p) => p.theme.colors.surface}88;
  min-width: 170px;
  backdrop-filter: blur(8px);
  transition: border-color 0.3s ease;
`;

const VmIdRow = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
`;

const StatusDot = styled.div<Transient<{ running: boolean }>>`
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background-color: ${(p) => (p.$running ? '#22c55e' : '#6b7280')};
  box-shadow: ${(p) =>
    p.$running ? '0 0 6px 1px #22c55e88' : 'none'};
  transition: background-color 0.3s ease;
`;

const VmId = styled.span`
  font-family: monospace;
  font-size: 0.82rem;
  font-weight: 700;
  color: ${(p) => p.theme.colors.text};
  letter-spacing: 0.5px;
`;

const VmDetail = styled.span`
  font-size: 0.78rem;
  color: ${(p) => p.theme.colors.text}bb;
  line-height: 1.4;
`;

const VmPid = styled.span`
  font-size: 0.7rem;
  color: ${(p) => p.theme.colors.text}55;
  font-family: monospace;
`;

const EmptyState = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 36px 0;
  color: ${(p) => p.theme.colors.text}55;
  font-size: 0.875rem;
`;

const FooterRow = styled.div`
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  padding-top: 4px;
  border-top: 1px solid ${(p) => p.theme.colors.text}18;
`;

const StatusBadge = styled.span<Transient<{ ok: boolean }>>`
  font-size: 0.75rem;
  color: ${(p) => (p.$ok ? '#22c55e' : '#ef4444')};
`;

// ── Helpers ────────────────────────────────────────────────────────────────────

function formatUptime(seconds: number | null): string {
  if (seconds === null) return '—';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// ── Sub-components ─────────────────────────────────────────────────────────────

const vmCardVariants = {
  initial: { opacity: 0, scale: 0.92 },
  animate: { opacity: 1, scale: 1 },
  exit: { opacity: 0, scale: 0.88 },
};

const VmCardItem: FC<{ vm: FirecrackerVm }> = ({ vm }) => (
  <VmCard
    key={vm.vm_id}
    $running={vm.running}
    variants={vmCardVariants}
    initial="initial"
    animate="animate"
    exit="exit"
    layout
    transition={{ duration: 0.2 }}
  >
    <VmIdRow>
      <StatusDot $running={vm.running} />
      <VmId>{vm.vm_id}</VmId>
    </VmIdRow>
    <VmDetail>🌐 {vm.ip}</VmDetail>
    <VmDetail>
      ⚡ {vm.vcpu_count} vCPU · {vm.mem_size_mib} MiB
    </VmDetail>
    <VmDetail>⏱ {formatUptime(vm.uptime_s)}</VmDetail>
    {vm.pid !== null && <VmPid>PID {vm.pid}</VmPid>}
  </VmCard>
);

// ── Widget ─────────────────────────────────────────────────────────────────────

type FirecrackerWidgetProps = {
  load: FirecrackerLoad | undefined;
  config: Config;
};

export const FirecrackerWidget: FC<FirecrackerWidgetProps> = ({ load }) => {
  const theme = useTheme();
  const vms = load?.vms ?? [];
  const runningCount = vms.filter((v) => v.running).length;

  const subText =
    vms.length === 0
      ? 'No VMs allocated'
      : `${runningCount} running · ${vms.length} allocated`;

  return (
    <Container>
      <Header>
        <IconBox $color={theme.colors.vmPrimary}>
          <FontAwesomeIcon icon={faFire} size="2x" />
        </IconBox>
        <HeadingGroup>
          <Heading>Firecracker VMs</Heading>
          <SubHeading>{subText}</SubHeading>
        </HeadingGroup>
      </Header>

      {vms.length === 0 ? (
        <EmptyState>No active VMs</EmptyState>
      ) : (
        <VmGrid>
          <AnimatePresence mode="popLayout">
            {vms.map((vm) => (
              <VmCardItem key={vm.vm_id} vm={vm} />
            ))}
          </AnimatePresence>
        </VmGrid>
      )}

      {load && (
        <FooterRow>
          <StatusBadge $ok={load.base_image_ok}>
            {load.base_image_ok ? '✓' : '✗'} Base image
          </StatusBadge>
          <StatusBadge $ok={load.kernel_ok}>
            {load.kernel_ok ? '✓' : '✗'} Kernel
          </StatusBadge>
        </FooterRow>
      )}
    </Container>
  );
};
