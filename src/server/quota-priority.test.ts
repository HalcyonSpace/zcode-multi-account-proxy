import {test,expect} from 'bun:test';
import {freeQuota} from './quota-priority';
test('matching model only; skip expired and exhausted quota',()=>{
 const q={balances:[{showName:'GLM-5.3-Flash',remainingUnits:50,expiresAt:200}]};
 expect(freeQuota(q,'glm-5.3',100)).toBeNull();expect(freeQuota(q,'glm-5.3-flash',100)).toBe(200);
 expect(freeQuota(q,'glm-5.3-flash',201)).toBeNull();
 expect(freeQuota({...q,plans:[{endsAt:99}]},'glm-5.3-flash',100)).toBeNull();
 expect(freeQuota({balances:[{showName:'GLM-5.3',remainingUnits:0}]},'glm-5.3',100)).toBeNull();
});
